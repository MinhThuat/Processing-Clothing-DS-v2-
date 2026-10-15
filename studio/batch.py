#!/usr/bin/env python3
"""Flat Studio batch — chạy qua đêm, không giao diện.

  python batch.py [--until 07:00] [--limit N] [--dry-run]
  python batch.py --selfcheck

Lark Base (view 1. DS Order) -> dòng No File chưa có Flat Pieces -> tải Link 1..N -> Claude chọn ảnh
-> flat_pipeline (pieces 3D + art) -> Claude QA, gen lại -> up Flat Pieces + ghi Flat Note.
Spec: docs/superpowers/specs/2026-10-15-batch-lark-flat-design.md
"""
import argparse, hashlib, io, json, os, re, shutil, subprocess, sys, time, traceback, urllib.request
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from PIL import Image

import judge
import lark_base as lb
from batch_errors import AUTH_RE, AuthError, QuotaError, StepError, raise_for

ROOT = Path(__file__).resolve().parent
DATA = Path(os.getenv("STUDIO_DATA", ROOT.parent / "flat_studio_data")).expanduser()
BATCH = DATA / "batch"
LOCK = DATA / "batch.lock"
PIPELINE = ROOT / "flat_pipeline.py"
GROUP_TIMEOUT = 45 * 60
QUOTA_WAIT, QUOTA_TRIES = 30 * 60, 3
UA = {"User-Agent": "Mozilla/5.0"}
_LOGF = None


MAX_ATTEMPTS = 4         # gen lại: lần 1-2 y nguyên, lần 3-4 có hint + chọn lại ảnh/crop
SAME_ERR_STOP = 5        # 5 nhóm liên tiếp lỗi cùng kiểu -> dừng đợt
_SIZE = re.compile(r"^(XXS|XS|S|M|L|XL|XXL|XXXL|\d{1,2}XL|\d{1,2}T|\d{1,2}Y|\d{1,2}-\d{1,2}Y?"
                   r"|one ?size|free ?size|\d+(\.\d+)?\s*(x\s*\d+(\.\d+)?)?\s*(in|inch|cm)?)$", re.I)


# ---------- tên ----------
def sku_stem(sku):
    """SKU Design bỏ mã lô (đoạn cuối) và size (đoạn kế cuối nếu là size)."""
    parts = [p.strip() for p in (sku or "").split("-")]
    if len(parts) > 1:
        parts = parts[:-1]
    if len(parts) > 1 and _SIZE.match(parts[-1]):
        parts = parts[:-1]
    return "-".join(parts)


def safe_name(s, maxlen=120):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", s or "").strip(" ._")
    return s[:maxlen] or "item"


def piece_filename(sku, slug, failed=False):
    return f"{safe_name(sku_stem(sku))}_{slug}{'_QAFAIL' if failed else ''}.png"


# ---------- hàng đợi ----------
def needs_work(row):
    return ("No File" in row["check_file"] and not row["has_pieces"] and bool(row["links"])
            and not row["note"].upper().startswith("SKIP"))


def build_queue(rows, day):
    """Gom dòng cần làm CỦA NGÀY `day` (YYYY-MM-DD theo Create Date) theo bộ link; mới nhất lên trước."""
    groups = {}
    for r in rows:
        if r["created"][:10] == day and needs_work(r):
            groups.setdefault(tuple(r["links"]), []).append(r)
    out = [{"links": list(k), "rows": v, "newest": max(x["created"] for x in v)} for k, v in groups.items()]
    out.sort(key=lambda g: g["newest"], reverse=True)
    return out


def group_id(g):
    h = hashlib.sha1("|".join(g["links"]).encode()).hexdigest()[:6]
    return f"{safe_name(sku_stem(g['rows'][0]['sku']), 60)}_{h}"


# ---------- ghi chú ----------
def note_ok(day, n_pieces, regen):
    s = f"OK {day} · {n_pieces} mảnh + art"
    if regen:
        s += " · gen lại: " + ", ".join(f"{k}×{v}" for k, v in sorted(regen.items()))
    return s


def note_fail(day, fails):
    body = " · ".join(f"{k} ({','.join(v) or '?'})" for k, v in sorted(fails.items()))
    return f"QA FAIL {day} · {body} sau {MAX_ATTEMPTS} lần"


# ---------- điều kiện dừng ----------
class Stopper:
    """--until HH:MM, --limit N (đếm nhóm đã gen, SKIP không tính), N nhóm liên tiếp lỗi cùng kiểu."""

    def __init__(self, until=None, limit=None, now=datetime.now):
        self.limit, self.now, self.done = limit, now, 0
        self.err_kind, self.err_run, self.deadline = None, 0, None
        if until:
            h, m = map(int, until.split(":"))
            t = now().replace(hour=h, minute=m, second=0, microsecond=0)
            self.deadline = t if t > now() else t + timedelta(days=1)

    def record(self, kind):
        """kind: None = gen xong (OK/QA FAIL), 'skip', hoặc tên lỗi StepError."""
        if kind == "skip":
            return                              # SKIP: không tính limit, không cắt chuỗi lỗi
        if kind is None:
            self.done += 1
            self.err_kind, self.err_run = None, 0
        elif kind == self.err_kind:
            self.err_run += 1
        else:
            self.err_kind, self.err_run = kind, 1

    def reason(self):
        if self.deadline and self.now() >= self.deadline:
            return f"hết giờ {self.deadline:%H:%M}"
        if self.limit and self.done >= self.limit:
            return f"đủ --limit {self.limit}"
        if self.err_run >= SAME_ERR_STOP:
            return f"{self.err_run} nhóm liên tiếp lỗi '{self.err_kind}'"
        return None


# ---------- máy gen lại ----------
def improve(names, qa_fn, regen_fn, rehint_fn, keep_fn):
    """QA + gen lại mảnh lỗi tối đa MAX_ATTEMPTS lần.
      qa_fn(todo) -> {name: {ok, score, errors}}   chấm bản hiện tại của các mảnh
      regen_fn(todo, attempt, hint)                gen lại (attempt 1..4; hint từ lần 3)
      rehint_fn({name: errors}) -> hint            gọi 1 lần trước lần 3 (viết hint + chọn lại ảnh/crop)
      keep_fn(name, attempt)                       lưu bản hiện tại để chọn bản tốt nhất
    -> best {name: (attempt, score)}, fails {name: errors} (vẫn lỗi), regen {name: số lần gen lại}."""
    best, last, regen = {}, {}, {}
    todo, hint = list(names), None
    for attempt in range(MAX_ATTEMPTS + 1):
        if attempt:
            if attempt == 3:
                hint = rehint_fn({n: last[n]["errors"] for n in todo})
            regen_fn(list(todo), attempt, hint)
            for n in todo:
                regen[n] = attempt
        res = qa_fn(list(todo))
        nxt = []
        for n in todo:
            r = res.get(n) or {"ok": False, "score": 0, "errors": []}
            last[n] = r
            keep_fn(n, attempt)
            if r["ok"]:
                best[n] = (attempt, r["score"])         # bản đạt luôn được chọn
            else:
                if n not in best or r["score"] > best[n][1]:
                    best[n] = (attempt, r["score"])
                nxt.append(n)
        todo = nxt
        if not todo:
            break
    return best, {n: last[n]["errors"] for n in todo}, regen


# ---------- log + báo Lark ----------
def log(stage, msg=""):
    line = f"[{datetime.now():%H:%M:%S}][{stage:<6}] {msg}"
    print(line, flush=True)
    if _LOGF:
        _LOGF.write(line + "\n"); _LOGF.flush()


def notify(text):
    """Gửi tin về bot Lark (LARK_WEBHOOK trong .env). Lỗi gửi chỉ log, không dừng đợt."""
    url = os.getenv("LARK_WEBHOOK")
    if not url:
        log("notify", "(chưa có LARK_WEBHOOK trong .env) " + text); return
    body = json.dumps({"msg_type": "text", "content": {"text": "🧵 Flat batch\n" + text}}).encode()
    try:
        req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        log("notify", f"gửi Lark lỗi: {e}")


# ---------- lock ----------
def _alive(pid):
    if sys.platform == "win32":     # os.kill(pid, 0) trên Windows là gửi Ctrl+C -> coi như còn sống
        return True
    try:
        os.kill(pid, 0); return True
    except OSError:
        return False


def acquire_lock(path=LOCK):
    if path.exists():
        try:
            pid = int(path.read_text().strip() or 0)
        except ValueError:
            pid = 0
        if pid and pid != os.getpid() and _alive(pid):
            sys.exit(f"Đang có batch khác chạy (PID {pid}). Chắc chắn không còn thì xoá {path}.")
        if pid == os.getpid():
            sys.exit(f"Lock {path} đã thuộc tiến trình này.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()))


def release_lock(path=LOCK):
    path.unlink(missing_ok=True)


# ---------- ảnh đầu vào ----------
def download(url, dst, tries=3):
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
                data = r.read(40 * 1024 * 1024)
            Image.open(io.BytesIO(data)).verify()
            dst.write_bytes(data)
            return dst
        except Exception as e:
            last = e
            time.sleep(2 * (i + 1))
    raise StepError("tải ảnh", f"{url[:80]}: {last}")


def make_input(src, crop, dst):
    """Ảnh đầu vào cho pipeline: crop theo bbox 0-1000 (bỏ qua nếu None hoặc vùng < 32px)."""
    im = Image.open(src).convert("RGB")
    if crop:
        w, h = im.size
        box = (crop[0] * w // 1000, crop[1] * h // 1000, crop[2] * w // 1000, crop[3] * h // 1000)
        if box[2] - box[0] >= 32 and box[3] - box[1] >= 32:
            im = im.crop(box)
    im.save(dst)
    return dst


# ---------- pipeline ----------
def run_pipe(args, logf, timeout):
    """Chạy flat_pipeline --single. Hết đăng nhập (trong log/stderr) -> AuthError; exit≠0 -> raise_for."""
    cmd = [sys.executable, str(PIPELINE), "--single", *map(str, args), "--log", str(logf)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=max(60, timeout), cwd=str(ROOT),
                           encoding="utf-8", errors="replace", env={**os.environ, "PYTHONUTF8": "1"})
    except subprocess.TimeoutExpired:
        raise StepError("quá thời gian", "pipeline")
    logtxt = logf.read_text(encoding="utf-8", errors="replace") if logf.exists() else ""
    if AUTH_RE.search(logtxt + (p.stderr or "")):
        raise AuthError("codex/ARK hết đăng nhập — xem " + str(logf))
    if p.returncode != 0:
        raise_for((p.stderr or "")[-2000:], "gen")
    return logtxt


def _gen_soft(fn):
    """Gen lỗi (StepError 'gen') -> chỉ log, để QA chấm thiếu file; lỗi khác ném tiếp."""
    try:
        fn()
    except StepError as e:
        if e.kind != "gen":
            raise
        log("gen", f"lỗi gen (tính 1 lần hỏng): {str(e)[:200]}")


# ---------- 1 nhóm con màu ----------
def process_subgroup(sub, rows, pick, srcs, sdir, day, dry, deadline):
    """Gen pieces 3D + art cho 1 nhóm con màu -> QA/gen lại -> up. Trả True nếu còn mảnh QA FAIL."""
    sdir.mkdir(parents=True, exist_ok=True)
    color = None if sub["matches_photo"] else sub["color"]
    colour_desc = color or "same as the reference photo"
    ctx = {"front": make_input(srcs[pick["front"] - 1], pick["crop"], sdir / "in_front.png"),
           "back": (make_input(srcs[pick["back"] - 1], None, sdir / "in_back.png")
                    if pick["mode"] == "two" else None),
           "hint": None}
    n = Counter()

    def pipe(args):
        left = deadline - time.monotonic()
        if left <= 0:
            raise StepError("quá thời gian", "nhóm > 45 phút")
        n["pipe"] += 1
        extra = (["--color", color] if color else []) + (["--hint", ctx["hint"]] if ctx["hint"] else [])
        return run_pipe(args + extra, sdir / f"pipe_{n['pipe']:02d}.log", left)

    def mode_args():
        return ((["--back", ctx["back"]] if ctx["back"] else [])
                + (["--combined"] if pick["mode"] == "combined" else []))

    def piece_path(name):
        return sdir / ("art.png" if name == "art" else f"final_{name}.png")

    def gen_pieces(only=None, cache=True):
        args = ["--pieces", "--front", ctx["front"], *mode_args(), "-o", sdir / "final.png", "--emit", sdir]
        if only:
            args += ["--only", ",".join(only)]
        if cache and only:
            args += ["--cache", sdir / "pieces.json"]
        _gen_soft(lambda: pipe(args))

    def gen_art():
        _gen_soft(lambda: pipe(["--art", "--front", ctx["front"], "-o", sdir / "art.png",
                                "--emit", sdir / "art_work"]))

    # lần 0
    gen_pieces()
    gen_art()
    names = sorted(p.stem[len("final_"):] for p in sdir.glob("final_*.png"))
    if not names:
        raise StepError("gen", "không ra mảnh nào")
    names.append("art")                     # art lỗi lần 0 -> QA chấm 0 -> được gen lại

    def qa_fn(todo):
        n["qa"] += 1
        have = [x for x in todo if piece_path(x).exists()]
        res = {x: {"ok": False, "score": 0.0, "errors": ["gen_loi"]} for x in todo if x not in have}
        if have:
            refs = [(ctx["front"], "REF front")] + ([(ctx["back"], "REF back")] if ctx["back"] else [])
            sheet = judge.contact_sheet([refs] + judge.grid([(piece_path(x), x) for x in have], cols=4),
                                        sdir / f"sheet_qa_{n['qa']}.jpg")
            res.update(judge.qa(sheet, have, colour_desc, len(refs)))
        (sdir / f"qa_{n['qa']}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        log("qa", " ".join(f"{k}:{'✓' if v['ok'] else '✗'}{v['score']:.0f}" for k, v in res.items()))
        return res

    def regen_fn(todo, attempt, hint):
        log("regen", f"lần {attempt}: {', '.join(todo)}" + (" (hint)" if hint else ""))
        for x in todo:
            piece_path(x).unlink(missing_ok=True)
        slugs = [x for x in todo if x != "art"]
        if slugs:
            gen_pieces(only=slugs, cache=attempt <= 2)     # lần 3-4: vision lại (+ hint, ảnh/crop mới)
        if "art" in todo:
            gen_art()

    def rehint_fn(fails):
        items = [(p, str(i)) for i, p in enumerate(srcs, 1)]
        items += [(piece_path(x), x) for x in fails if piece_path(x).exists()]
        sheet = judge.contact_sheet(judge.grid(items), sdir / "sheet_rehint.jpg")
        r = judge.rehint(sheet, fails, len(srcs))
        (sdir / "rehint.json").write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
        ctx["front"] = make_input(srcs[(r["front"] or pick["front"]) - 1], r["crop"], sdir / "in_front_r3.png")
        ctx["hint"] = r["hint"] or None
        log("regen", f"hint: {ctx['hint']}")
        return ctx["hint"]

    def keep_fn(name, attempt):
        src = piece_path(name)
        if src.exists():
            (sdir / "attempts").mkdir(exist_ok=True)
            shutil.copyfile(src, sdir / "attempts" / f"{name}_{attempt}.png")

    best, fails, regen = improve(names, qa_fn, regen_fn, rehint_fn, keep_fn)
    out = sdir / "out"; out.mkdir(exist_ok=True)
    files = []
    for name in names:
        src = sdir / "attempts" / f"{name}_{best[name][0]}.png"
        if src.exists():                    # mảnh chưa từng ra file thì không có gì để up
            dst = out / piece_filename(rows[0]["sku"], name, failed=name in fails)
            shutil.copyfile(src, dst); files.append(dst)
    if not files:
        raise StepError("gen", "không có mảnh nào để up")
    n_pieces = sum(1 for x in names if x != "art")
    note = note_fail(day, fails) if fails else note_ok(day, n_pieces, regen)
    upload_rows(rows, files, note, dry)
    return bool(fails)


def upload_rows(rows, files, note, dry):
    """Up cùng bộ file cho từng dòng; dòng đã có Flat Pieces -> bỏ qua. Ghi Note sau khi up xong."""
    if dry:
        log("dry", f"[không ghi Base] {len(rows)} dòng · {len(files)} file · note: {note}")
        return
    names = {f.name for f in files}
    done = {}
    for r in rows:
        rid = r["record_id"]
        if lb.get_row(rid)["has_pieces"]:
            log("up", f"{rid} đã có Flat Pieces -> bỏ qua"); continue
        try:
            lb.upload_files(rid, files)
        except StepError as e:
            toks = [p["file_token"] for p in lb.get_row(rid)["pieces"] if p.get("name") in names]
            if toks:
                lb.remove_files(rid, toks)
            if done:
                lb.write_notes(done)
            raise StepError("up", f"{rid}: {e}")
        done[rid] = note
        log("up", f"{rid} ✓ {len(files)} file")
    if done:
        lb.write_notes(done)


# ---------- 1 nhóm ----------
def cleanup(gdir):
    """Sau khi up xong: xoá ảnh tải về + bản trung gian; giữ out/, contact sheet, json, log."""
    for p in list(gdir.glob("src_*")) + list(gdir.glob("c*/attempts")) + list(gdir.glob("c*/art_work")):
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    for p in gdir.glob("c*/*.png"):
        p.unlink(missing_ok=True)


def process_group(g, rundir, day, dry, stats):
    """-> None (gen xong), 'skip', hoặc kind của StepError. QuotaError/AuthError ném tiếp lên main."""
    gid = group_id(g)
    gdir = rundir / gid; gdir.mkdir(parents=True, exist_ok=True)
    rows, deadline, done_ids = g["rows"], time.monotonic() + GROUP_TIMEOUT, set()
    log("group", f"=== {gid} · {len(rows)} dòng · {len(g['links'])} link · {g['newest'][:10]} ===")
    try:
        srcs = []
        for i, u in enumerate(g["links"], 1):
            try:
                srcs.append(download(u, gdir / f"src_{i:02d}.jpg"))
            except StepError as e:
                log("tải", str(e))
        if not srcs:
            raise StepError("tải ảnh", "mọi link đều lỗi")
        sheet = judge.contact_sheet(judge.grid([(p, str(i)) for i, p in enumerate(srcs, 1)]),
                                    gdir / "sheet_pick.jpg")
        pick = judge.pick(sheet, rows, len(srcs))
        (gdir / "pick.json").write_text(json.dumps(pick, ensure_ascii=False, indent=1), encoding="utf-8")
        if pick["skip"]:
            note = f"SKIP: {pick['skip']}"
            log("skip", note)
            if not dry:
                lb.write_notes({r["record_id"]: note for r in rows})
            stats["skip"] += 1
            return "skip"
        log("pick", f"front={pick['front']} back={pick['back']} mode={pick['mode']} "
                    f"màu={[c['color'] for c in pick['colors']]}")
        any_fail = False
        for k, sub in enumerate(pick["colors"], 1):
            sub_rows = [r for r in rows if r["record_id"] in sub["record_ids"]]
            any_fail |= process_subgroup(sub, sub_rows, pick, srcs, gdir / f"c{k}", day, dry, deadline)
            done_ids |= {r["record_id"] for r in sub_rows}
        stats["qafail" if any_fail else "ok"] += 1
        if not dry:
            cleanup(gdir)
        return None
    except (QuotaError, AuthError):
        raise
    except Exception as e:                 # StepError, hoặc lỗi lạ (ảnh hỏng, PIL...) -> chỉ bỏ nhóm này
        kind = e.kind if isinstance(e, StepError) else "lỗi khác"
        log("lỗi", f"{gid}: {e}" + ("" if isinstance(e, StepError) else "\n" + traceback.format_exc()))
        stats["loi"] += 1
        left = {r["record_id"]: f"LỖI {day}: {kind}" for r in rows if r["record_id"] not in done_ids}
        if left and not dry:
            try:
                lb.write_notes(left)
            except StepError as e2:
                log("lỗi", f"ghi note LỖI thất bại: {e2}")
        return kind


# ---------- main ----------
def main():
    global _LOGF
    ap = argparse.ArgumentParser(description="Flat batch: Lark Base -> mảnh rập, chạy qua đêm")
    ap.add_argument("--until", help="dừng sau giờ HH:MM (làm nốt nhóm đang chạy)")
    ap.add_argument("--limit", type=int, help="dừng sau N nhóm đã gen (SKIP không tính)")
    ap.add_argument("--dry-run", action="store_true", help="làm đủ trừ ghi Lark Base")
    ap.add_argument("--date", help="chỉ làm mẫu có Create Date = ngày này (YYYY-MM-DD); mặc định hôm nay")
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args()
    if a.selfcheck:
        import batch_errors
        batch_errors._selfcheck(); lb._selfcheck(); judge._selfcheck(); _selfcheck()
        return
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    acquire_lock()
    day = date.today().isoformat()          # ngày ghi Note + thư mục log
    only_day = a.date or day                 # chỉ làm mẫu tạo ngày này (mẫu ngày cũ bỏ qua)
    rundir = BATCH / day; rundir.mkdir(parents=True, exist_ok=True)
    _LOGF = open(rundir / "batch.log", "a", encoding="utf-8")
    stats, stop, reason = Counter(), Stopper(a.until, a.limit), "hết việc"
    log("start", f"date={only_day} until={a.until} limit={a.limit} dry={a.dry_run}")
    try:
        queue = build_queue(lb.fetch_rows(), only_day)
        log("queue", f"{len(queue)} nhóm cần làm (mẫu ngày {only_day})")
        i, waits = 0, 0
        while i < len(queue):
            try:
                kind = process_group(queue[i], rundir, day, a.dry_run, stats)
                waits = 0
            except QuotaError as e:
                waits += 1
                if waits > QUOTA_TRIES:
                    reason = f"hết hạn mức sau {QUOTA_TRIES} lần chờ ({str(e)[:120]})"; break
                notify(f"⏸ Hết hạn mức — chờ 30' rồi thử lại (lần {waits}/{QUOTA_TRIES})\n{str(e)[:200]}")
                log("quota", f"chờ {QUOTA_WAIT // 60}' (lần {waits}/{QUOTA_TRIES}): {e}")
                time.sleep(QUOTA_WAIT)
                continue                       # làm lại nhóm này
            i += 1
            stop.record(kind)
            r = stop.reason()
            if r:
                reason = r; break
    except AuthError as e:
        reason = f"hết đăng nhập: {str(e)[:200]}"
    except KeyboardInterrupt:
        reason = "Ctrl+C"
    except Exception as e:
        reason = f"lỗi không lường: {e}"
        log("crash", traceback.format_exc())
    finally:
        summary = (f"Đợt {day}{' (dry-run)' if a.dry_run else ''}: {stats['ok']} OK · {stats['qafail']} QA FAIL"
                   f" · {stats['skip']} SKIP · {stats['loi']} LỖI · dừng vì: {reason}"
                   f" · log: {rundir / 'batch.log'}")
        log("done", summary)
        notify(summary)
        release_lock()


def _selfcheck():
    # tên file: bỏ mã lô + size; ký tự cấm -> _
    assert sku_stem("HKH032604A21-Black-2D Hoodie-L-G130426") == "HKH032604A21-Black-2D Hoodie"
    assert sku_stem("VTY022506A07-Christoph Stelzer1-Button Shirt-2XL-J150426") == \
        "VTY022506A07-Christoph Stelzer1-Button Shirt"
    assert sku_stem("VPC132605A14-Austin 080626-Car visor clip-AP8626") == \
        "VPC132605A14-Austin 080626-Car visor clip"
    assert sku_stem("X") == "X"
    assert piece_filename("A/B-Black-Tee-M-G1", "tay_sau", failed=True) == "A_B-Black-Tee_tay_sau_QAFAIL.png"
    assert piece_filename("A-Tee-S-G1", "art") == "A-Tee_art.png"
    # hàng đợi: lọc, chỉ đúng ngày `day`, gom theo bộ link, mới nhất trước
    def row(rid, links, created, **kw):
        return {"record_id": rid, "sku": f"S-{rid}-Tee-M-G1", "color": "", "created": created,
                "check_file": kw.get("cf", ["No File"]), "links": links,
                "has_pieces": kw.get("hp", False), "pieces": [], "note": kw.get("note", "")}
    rows = [row("a", ["u1"], "2026-04-09T01:00:00.000+07:00"),
            row("b", ["u1"], "2026-04-09T03:00:00.000+07:00"),
            row("c", ["u2"], "2026-04-09T02:00:00.000+07:00"),
            row("d", ["u3"], "2026-04-09T09:00:00.000+07:00", hp=True),
            row("e", ["u4"], "2026-04-09T09:00:00.000+07:00", note="SKIP: mug"),
            row("f", ["u5"], "2026-04-09T09:00:00.000+07:00", cf=["Urgent"]),
            row("g", [], "2026-04-09T09:00:00.000+07:00"),
            row("h", ["u6"], "2026-04-09T09:00:00.000+07:00", note="LỖI 2026-10-15: tải ảnh"),
            row("i", ["u7"], "2026-04-08T23:59:00.000+07:00"),          # ngày cũ -> không chạy
            row("j", ["u1"], "2026-04-10T00:01:00.000+07:00")]          # ngày khác -> không chạy
    q = build_queue(rows, "2026-04-09")
    assert [[r["record_id"] for r in g["rows"]] for g in q] == [["h"], ["a", "b"], ["c"]]
    assert q[1]["newest"] == "2026-04-09T03:00:00.000+07:00"
    assert build_queue(rows, "2026-04-08")[0]["rows"][0]["record_id"] == "i"
    assert group_id(q[1]).startswith("S-a-Tee_") and len(group_id(q[1])) <= 80
    # ghi chú
    assert note_ok("2026-10-16", 6, {"tay_sau": 1}) == "OK 2026-10-16 · 6 mảnh + art · gen lại: tay_sau×1"
    assert note_ok("2026-10-16", 4, {}) == "OK 2026-10-16 · 4 mảnh + art"
    assert note_fail("2026-10-16", {"tay_sau": ["sau_giong_truoc"]}) == \
        "QA FAIL 2026-10-16 · tay_sau (sau_giong_truoc) sau 4 lần"
    # dừng: limit đếm nhóm đã gen (skip không tính); 5 lỗi liên tiếp cùng kiểu
    s = Stopper(None, 2)
    s.record("skip"); assert s.reason() is None
    s.record(None); s.record(None); assert s.reason() == "đủ --limit 2"
    s = Stopper(None, None)
    for _ in range(4):
        s.record("tải ảnh")
    assert s.reason() is None
    s.record("skip"); s.record("tải ảnh"); assert s.reason() == "5 nhóm liên tiếp lỗi 'tải ảnh'"
    s = Stopper(None, None)
    for k in ["gen", "gen", "judge", "gen", "gen", None, "gen"]:
        s.record(k)
    assert s.reason() is None
    now = [datetime(2026, 10, 15, 23, 0)]
    s = Stopper("07:00", None, now=lambda: now[0])
    assert s.reason() is None
    now[0] = datetime(2026, 10, 16, 7, 0); assert s.reason() == "hết giờ 07:00"
    # máy gen lại: A đạt ngay; B đạt ở lần 3 (sau hint); C lỗi cả 5 bản -> chọn bản điểm cao nhất
    script = {"A": [(True, 9)], "B": [(False, 3), (False, 4), (False, 2), (True, 8)],
              "C": [(False, 2), (False, 6), (False, 3), (False, 5), (False, 4)]}
    tries = Counter(); calls = {"regen": [], "hint": 0, "keep": []}
    def qa_fn(todo):
        out = {}
        for n in todo:
            ok, sc = script[n][tries[n]]; tries[n] += 1
            out[n] = {"ok": ok, "score": sc, "errors": [] if ok else ["mo_nhoe"]}
        return out
    def regen_fn(todo, attempt, hint):
        calls["regen"].append((tuple(todo), attempt, hint))
    def rehint_fn(fails):
        calls["hint"] += 1; assert set(fails) == {"B", "C"}; return "FIX IT"
    def keep_fn(n, attempt):
        calls["keep"].append((n, attempt))
    best, fails, regen = improve(["A", "B", "C"], qa_fn, regen_fn, rehint_fn, keep_fn)
    assert best == {"A": (0, 9), "B": (3, 8), "C": (1, 6)}
    assert fails == {"C": ["mo_nhoe"]} and regen == {"B": 3, "C": 4}
    assert calls["hint"] == 1
    assert calls["regen"] == [(("B", "C"), 1, None), (("B", "C"), 2, None), (("B", "C"), 3, "FIX IT"),
                              (("C",), 4, "FIX IT")]
    assert ("A", 0) in calls["keep"] and ("C", 4) in calls["keep"]
    # crop theo bbox 0-1000; bbox quá nhỏ/None -> giữ nguyên ảnh
    import tempfile
    from PIL import Image as _I
    td = Path(tempfile.mkdtemp()); src = td / "s.jpg"; _I.new("RGB", (1000, 500), "white").save(src)
    assert _I.open(make_input(src, [100, 200, 900, 800], td / "a.png")).size == (800, 300)
    assert _I.open(make_input(src, None, td / "b.png")).size == (1000, 500)
    assert _I.open(make_input(src, [100, 100, 101, 101], td / "c.png")).size == (1000, 500)
    # lock: PID đã chết -> tự gỡ; PID sống -> chặn
    lk = td / "x.lock"; lk.write_text("999999999")
    acquire_lock(lk); assert lk.read_text() == str(os.getpid())
    try:
        acquire_lock(lk); raise AssertionError("lock sống phải chặn")
    except SystemExit:
        pass
    release_lock(lk); assert not lk.exists()
    print("batch selfcheck OK")


if __name__ == "__main__":
    main()
