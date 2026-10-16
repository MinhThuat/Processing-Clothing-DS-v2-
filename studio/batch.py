#!/usr/bin/env python3
"""Flat Studio batch — chạy qua đêm, không giao diện.

  python batch.py [--until 07:00] [--limit N] [--dry-run]
  python batch.py --selfcheck

Lark Base (view 1. DS Order) -> dòng No File chưa có Flat Pieces -> tải Link 1..N -> Claude chọn ảnh
-> flat_pipeline (pieces 3D + art) -> Claude QA, gen lại -> up Flat Pieces + ghi Flat Note.
Spec: docs/superpowers/specs/2026-10-15-batch-lark-flat-design.md
"""
import argparse, hashlib, io, json, os, re, shutil, signal, subprocess, sys, time, traceback, urllib.request
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from PIL import Image

import judge
import lark_base as lb
from batch_errors import AUTH_HARD_RE, AUTH_RE, QUOTA_RE, AuthError, QuotaError, StepError, raise_for
from flat_pipeline import _pieces_plan

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


def _quota_wait(stop, seconds, sleep=time.sleep):
    """Chờ hạn mức `seconds' giây, chia đoạn <=60s; thoát sớm & trả lý do ngay khi stop.reason()
    có (vd hết giờ --until trong lúc chờ), không chờ hết cả 30'. -> lý do dừng, hoặc None nếu
    chờ hết mà không có lý do nào (đến lúc đó thì cứ thử lại như cũ)."""
    waited = 0
    while waited < seconds:
        r = stop.reason()
        if r:
            return r
        chunk = min(60, seconds - waited)
        sleep(chunk)
        waited += chunk
    return stop.reason()


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
    try:
        print(line, flush=True)
    except OSError:
        pass
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
    logf.unlink(missing_ok=True)   # pipeline ghi log kiểu append -> chỉ đọc log của lần chạy này
    cmd = [sys.executable, str(PIPELINE), "--single", *map(str, args), "--log", str(logf)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=max(60, timeout), cwd=str(ROOT),
                           encoding="utf-8", errors="replace", env={**os.environ, "PYTHONUTF8": "1"})
    except subprocess.TimeoutExpired:
        raise StepError("quá thời gian", "pipeline")
    logtxt = logf.read_text(encoding="utf-8", errors="replace") if logf.exists() else ""
    if _log_auth_error(logtxt, p.stderr):
        raise AuthError("codex/ARK hết đăng nhập — xem " + str(logf))
    if _piece_quota(logtxt):
        raise QuotaError("hết hạn mức gen giữa chừng — xem " + str(logf))
    if p.returncode != 0:
        raise_for((p.stderr or "")[-2000:], "gen")
    return logtxt


def _piece_quota(logtxt):
    """True nếu có dòng gen mảnh lỗi ('✗ mảnh ...') khớp QUOTA_RE -> hạn mức hỏng giữa chừng.
    Chỉ tính dòng mảnh thật sự lỗi (không tính 429 mà OpenArt/codex tự hồi phục)."""
    return any("✗ mảnh" in line and QUOTA_RE.search(line) for line in logtxt.splitlines())


def _piece_auth(logtxt):
    """Mirror _piece_quota: dòng '✗ mảnh ...' khớp AUTH_RE (đầy đủ) -> mảnh lỗi vì hết đăng nhập
    thật sự (không phải 401 thoáng qua rồi tự hồi phục như HTTP 401 ở log vision)."""
    return any("✗ mảnh" in line and AUTH_RE.search(line) for line in logtxt.splitlines())


def _log_auth_error(logtxt, stderr=""):
    """Toàn log chỉ dừng trên câu CHẮC CHẮN hết đăng nhập (AUTH_HARD_RE) — codex vision log
    'HTTP 401' thoáng qua rồi tự refresh token không được tính; nhưng nếu chính dòng '✗ mảnh ...'
    (mảnh thật sự lỗi) khớp AUTH_RE đầy đủ thì vẫn coi là hết đăng nhập."""
    return bool(AUTH_HARD_RE.search(logtxt + (stderr or ""))) or _piece_auth(logtxt)


def _gen_soft(fn):
    """Gen lỗi (StepError 'gen') -> chỉ log, để QA chấm thiếu file; lỗi khác ném tiếp."""
    try:
        fn()
    except StepError as e:
        if e.kind != "gen":
            raise
        log("gen", f"lỗi gen (tính 1 lần hỏng): {str(e)[:200]}")


def _pieces_json_slugs(path):
    """pieces.json -> tập slug mảnh (không gồm 'art') theo plan hiện tại (flat_pipeline._pieces_plan)."""
    return {p["slug"] for p in _pieces_plan(json.loads(Path(path).read_text(encoding="utf-8")))}


def _slugs_match(pieces_json_path, names):
    """True nếu bộ mảnh trong pieces.json (bỏ 'art') khớp đúng tập `names` (cũng bỏ 'art').
    Dùng ở lần gen lại 3: vision lại có thể đổi loại đồ/tay giống-khác -> đổi tên mảnh."""
    return _pieces_json_slugs(pieces_json_path) == set(names)


# ---------- 1 nhóm con (biến thể thiết kế + màu) ----------
def process_subgroup(group, rows, pick, srcs, sdir, day, dry, deadline, done_ids):
    """Gen pieces 3D + art cho 1 nhóm con (biến thể + màu) -> QA/gen lại -> up. Trả True nếu còn mảnh QA FAIL."""
    shutil.rmtree(sdir, ignore_errors=True)    # thử lại cùng ngày: bỏ log/mảnh/pieces.json của lần trước
    sdir.mkdir(parents=True)
    color = None if group["matches_photo"] else group["color"]
    colour_desc = color or "same as the reference photo"
    ctx = {"front": make_input(srcs[group["front"] - 1], group["crop"], sdir / "in_front.png"),
           "front_src": srcs[group["front"] - 1],        # ảnh gốc chưa cắt, để QA luôn so với ảnh thật
           "cropped": group["crop"] is not None,
           "back": (make_input(srcs[group["back"] - 1], None, sdir / "in_back.png")
                    if group["mode"] == "two" else None),
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
                + (["--combined"] if group["mode"] == "combined" else []))

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
    pj = sdir / "pieces.json"
    if not pj.exists():
        raise StepError("gen", "vision không ra pieces.json")
    want = {p["slug"] for p in _pieces_plan(json.loads(pj.read_text(encoding="utf-8")))}
    have = {p.stem[len("final_"):] for p in sdir.glob("final_*.png")}
    names = sorted(want | have)              # mảnh lỗi lần 0 (thiếu file) vẫn vào danh sách -> QA chấm 0 -> gen lại
    if not names:
        raise StepError("gen", "không ra mảnh nào")
    names.append("art")                     # art lỗi lần 0 -> QA chấm 0 -> được gen lại

    def qa_fn(todo):
        n["qa"] += 1
        have = [x for x in todo if piece_path(x).exists()]
        # mảnh chưa ra file (gen lỗi) -> -1, không phải 0, để bản đã từng ra file (dù điểm 0) vẫn thắng
        res = {x: {"ok": False, "score": -1.0, "errors": ["gen_loi"]} for x in todo if x not in have}
        if have:
            # luôn so với ảnh gốc chưa cắt (QA phải thấy nguyên bộ/toàn ảnh, không chỉ phần đã cắt
            # -- bbox ước lượng từ contact sheet không đáng tin); kèm bản đã cắt nếu có cắt thật
            refs = ([(ctx["front_src"], "REF photo")]
                    + ([(ctx["front"], "REF crop")] if ctx["cropped"] else [])
                    + ([(ctx["back"], "REF back")] if ctx["back"] else []))
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
            if attempt == 3:
                # lần 3: vision lại (crop + hint mới, từ rehint_fn) -> có thể đổi loại đồ/tay
                # giống-khác -> tên mảnh lệch với bản gốc (USER DECISION: khi lệch thì quay về
                # mô tả cũ, giữ crop + hint mới).
                v0 = sdir / "pieces_v0.json"
                shutil.copyfile(pj, v0)
                gen_pieces(only=slugs, cache=False)
                old = {n for n in names if n != "art"}
                if pj.exists() and not _slugs_match(pj, old):
                    new = _pieces_json_slugs(pj)
                    log("regen", f"lần 3 vision ra mảnh khác ({sorted(new)} != {sorted(old)})"
                                 " -> quay lại mô tả cũ, giữ crop+hint mới")
                    for p in sdir.glob("final_*.png"):
                        if p.stem[len("final_"):] not in old:
                            p.unlink(missing_ok=True)
                    shutil.copyfile(v0, pj)
                    missing = [x for x in slugs if not piece_path(x).exists()]
                    if missing:
                        gen_pieces(only=missing, cache=True)
            else:
                gen_pieces(only=slugs, cache=True)     # lần 1-2: y nguyên; lần 4: dùng pieces.json hiện tại
        if "art" in todo:
            gen_art()

    def rehint_fn(fails):
        items = [(p, str(i)) for i, p in enumerate(srcs, 1)]
        items += [(piece_path(x), x) for x in fails if piece_path(x).exists()]
        sheet = judge.contact_sheet(judge.grid(items), sdir / "sheet_rehint.jpg")
        try:
            r = judge.rehint(sheet, fails, len(srcs))
        except StepError as e:               # judge lỗi -> không phá nhóm, gen lại y nguyên crop cũ
            log("regen", f"rehint lỗi, giữ ảnh/crop cũ: {e}")
            ctx["hint"] = None
            return None
        (sdir / "rehint.json").write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
        front_idx = (r["front"] or group["front"]) - 1
        crop = group["crop"] if group["mode"] == "combined" else r["crop"]  # combined: giữ vùng cắt phiên bản (nửa MEN/WOMEN)
        ctx["front_src"] = srcs[front_idx]
        ctx["front"] = make_input(srcs[front_idx], crop, sdir / "in_front_r3.png")
        ctx["cropped"] = crop is not None
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
    manual = pick.get("manual") or {}
    notes = {r["record_id"]: note + (f" · CẦN CHỈNH: {manual[r['record_id']]}"
                                     if r["record_id"] in manual else "") for r in rows}
    upload_rows(rows, files, notes, dry, done_ids)
    return bool(fails)


def upload_rows(rows, files, notes, dry, done_ids):
    """Up cùng bộ file cho từng dòng, mỗi dòng ghi Note riêng (`notes`: record_id -> text, có thể khác
    nhau khi có 'CẦN CHỈNH'); dòng đã có Flat Pieces -> bỏ qua.
    `done_ids` (set dùng chung của cả nhóm) được cập nhật ngay khi 1 dòng up xong, để lỗi giữa chừng
    (quota/lỗi lạ ở dòng sau) không mất note của các dòng đã up trước đó, và không bị ghi LỖI nhầm."""
    if dry:
        log("dry", f"[không ghi Base] {len(rows)} dòng · {len(files)} file · note: {notes}")
        return
    names = {f.name for f in files}
    done = {}
    try:
        for r in rows:
            rid = r["record_id"]
            if lb.get_row(rid)["has_pieces"]:
                log("up", f"{rid} đã có Flat Pieces -> bỏ qua"); continue
            try:
                lb.upload_files(rid, files)
            except BaseException as e:      # Ctrl+C/SIGTERM giữa chừng cũng phải dọn file dở
                try:
                    toks = [p["file_token"] for p in lb.get_row(rid)["pieces"] if p.get("name") in names]
                    if toks:
                        lb.remove_files(rid, toks)
                except Exception as e2:
                    log("up", f"{rid} dọn file dở lỗi: {e2}")
                if isinstance(e, (AuthError, QuotaError, KeyboardInterrupt, SystemExit)):
                    raise
                raise StepError("up", f"{rid}: {e}")
            done[rid] = notes[rid]
            done_ids.add(rid)
            log("up", f"{rid} ✓ {len(files)} file")
    finally:
        if done:
            try:
                lb.write_notes(done)
            except Exception as e:
                log("up", f"ghi note up thất bại: {e}")


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
        log("pick", "; ".join(f"{g['variant']}/{g['color']}: front={g['front']} back={g['back']} "
                              f"mode={g['mode']}" for g in pick["groups"]))
        any_fail = False
        for k, group in enumerate(pick["groups"], 1):
            sub_rows = [r for r in rows if r["record_id"] in group["record_ids"]]
            any_fail |= process_subgroup(group, sub_rows, pick, srcs, gdir / f"c{k}", day, dry, deadline, done_ids)
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


_UNTIL_RE = re.compile(r"^\d{1,2}:\d{2}$")


def _parse_args(argv=None):
    """Đọc cờ dòng lệnh + validate --date/--until TRƯỚC acquire_lock (sai thì ap.error -> SystemExit,
    không giữ lock treo)."""
    ap = argparse.ArgumentParser(description="Flat batch: Lark Base -> mảnh rập, chạy qua đêm")
    ap.add_argument("--until", help="dừng sau giờ HH:MM (làm nốt nhóm đang chạy)")
    ap.add_argument("--limit", type=int, help="dừng sau N nhóm đã gen (SKIP không tính)")
    ap.add_argument("--dry-run", action="store_true", help="làm đủ trừ ghi Lark Base")
    ap.add_argument("--date", help="chỉ làm mẫu có Create Date = ngày này (YYYY-MM-DD); mặc định hôm nay")
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args(argv)
    if a.date:
        try:
            date.fromisoformat(a.date)
        except ValueError:
            ap.error(f"--date không hợp lệ (cần YYYY-MM-DD): {a.date}")
    if a.until:
        m = _UNTIL_RE.match(a.until)
        if not m or not (0 <= int(a.until.split(":")[0]) <= 23 and 0 <= int(a.until.split(":")[1]) <= 59):
            ap.error(f"--until không hợp lệ (cần HH:MM): {a.until}")
    return a


def _raise_kbd(signum, frame):
    raise KeyboardInterrupt


# ---------- main ----------
def main():
    global _LOGF
    a = _parse_args()
    if a.selfcheck:
        import batch_errors
        batch_errors._selfcheck(); lb._selfcheck(); judge._selfcheck(); _selfcheck()
        return
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    # terminal đóng (SIGHUP) / dừng tiến trình (SIGTERM) -> coi như Ctrl+C: vào finally, dọn lock + báo Lark
    signal.signal(signal.SIGTERM, _raise_kbd)
    if sys.platform != "win32":
        signal.signal(signal.SIGHUP, _raise_kbd)
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
                r = _quota_wait(stop, QUOTA_WAIT)     # thoát sớm nếu --until/--limit/chuỗi lỗi chạm trong lúc chờ
                if r:
                    reason = r; break
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
        try:
            log("done", summary)
            notify(summary)
        finally:
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
    # _quota_wait: deadline đã qua -> thoát ngay, không sleep lần nào
    calls_sleep = []
    assert _quota_wait(s, 1800, sleep=lambda sec: calls_sleep.append(sec)) == "hết giờ 07:00"
    assert calls_sleep == []
    # không có lý do dừng -> chờ hết, chia đoạn <=60s
    s2 = Stopper(None, None)
    calls_sleep2 = []
    assert _quota_wait(s2, 150, sleep=lambda sec: calls_sleep2.append(sec)) is None
    assert calls_sleep2 == [60, 60, 30]
    # lý do dừng xuất hiện GIỮA lúc chờ -> thoát sớm (không chờ hết 150s)
    s3 = Stopper(None, 1)
    calls_sleep3 = []
    def sleep3(sec):
        calls_sleep3.append(sec)
        s3.record(None)              # giả lập: đủ --limit ngay trong lúc chờ
    assert _quota_wait(s3, 150, sleep=sleep3) == "đủ --limit 1"
    assert calls_sleep3 == [60]
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
    if sys.platform != "win32":              # PID khác nhưng còn sống (PID cha) -> cũng phải chặn
        lk2 = td / "y.lock"; lk2.write_text(str(os.getppid()))
        try:
            acquire_lock(lk2); raise AssertionError("lock PID sống khác phải chặn")
        except SystemExit:
            pass
        lk2.unlink()
    # _piece_quota: chỉ tính dòng "✗ mảnh ..." khớp QUOTA_RE
    assert _piece_quota("[gen] ✗ mảnh ao_sau lỗi: HTTP 429 usage_limit") is True
    assert _piece_quota("[vision] lỗi codex: HTTP 429") is False
    assert _piece_quota("[gen] ✗ mảnh x lỗi: openart không tạo file") is False
    # #1 401 giả (codex vision tự hồi phục) không được dừng run; chỉ câu CHẮC CHẮN mới dừng
    assert _log_auth_error("[vision   ] lỗi codex: HTTP 401 Unauthorized") is False
    assert _log_auth_error("[gen] ✗ mảnh ao_sau lỗi: token refresh failed") is True
    assert _log_auth_error("bình thường", stderr="codex chưa đăng nhập") is True   # AUTH_HARD_RE toàn log
    assert _piece_auth("[gen] ✗ mảnh ao_sau lỗi: token refresh failed") is True
    assert _piece_auth("[vision] lỗi codex: HTTP 401") is False       # không phải dòng "✗ mảnh"
    # #2 _slugs_match: lần 3 vision lại đổi tay giống<->khác -> tên mảnh lệch
    tb = dict(type="top_bottom", top_front="a", top_back="b", pants_front="c", pants_back="d",
              sl_front="e", sl_back="f", sr_front="g", sr_back="h")
    same_names = {"ao_truoc", "ao_sau", "quan_truoc", "quan_sau", "tay_truoc", "tay_sau"}
    diff_names = {"ao_truoc", "ao_sau", "quan_truoc", "quan_sau",
                  "tay_trai_truoc", "tay_trai_sau", "tay_phai_truoc", "tay_phai_sau"}
    p_same = td / "pieces_same.json"; p_same.write_text(json.dumps({**tb, "sleeves_same": True}))
    p_diff = td / "pieces_diff.json"; p_diff.write_text(json.dumps({**tb, "sleeves_same": False}))
    assert _slugs_match(p_same, same_names) is True
    assert _slugs_match(p_diff, same_names) is False
    assert _slugs_match(p_diff, diff_names) is True
    # upload_rows: lỗi giữa chừng không mất note dòng đã up, không ghi LỖI nhầm dòng đã up
    import lark_base as _lb
    orig = (_lb.get_row, _lb.upload_files, _lb.remove_files, _lb.write_notes)
    try:
        rows = [{"record_id": "r1"}, {"record_id": "r2"}, {"record_id": "r3"}]
        files = [td / "final_a.png"]; files[0].write_text("x")
        calls = {"remove": [], "notes": None}
        def fake_get_row(rid):
            return {"has_pieces": False, "pieces": [{"name": "final_a.png", "file_token": f"tok_{rid}"}]}
        def fake_upload(rid, fs):
            if rid == "r2":
                raise QuotaError("hết hạn mức")
        def fake_remove(rid, toks):
            calls["remove"].append((rid, toks))
        def fake_write_notes(notes):
            calls["notes"] = dict(notes)
        _lb.get_row, _lb.upload_files, _lb.remove_files, _lb.write_notes = \
            fake_get_row, fake_upload, fake_remove, fake_write_notes
        # notes: dict record_id -> note riêng (khác nhau mỗi dòng, vd dòng có "CẦN CHỈNH")
        notes1 = {"r1": "note1-A", "r2": "note1-B", "r3": "note1-C"}
        done_ids = set()
        try:
            upload_rows(rows, files, notes1, False, done_ids)
            raise AssertionError("QuotaError phải ném tiếp")
        except QuotaError:
            pass
        assert done_ids == {"r1"}
        assert calls["notes"] == {"r1": "note1-A"}          # đúng note riêng của r1, không phải của r2/r3
        assert calls["remove"] == [("r2", ["tok_r2"])]
        # lỗi lạ (không phải StepError) -> StepError kind "up"
        def fake_upload2(rid, fs):
            if rid == "r2":
                raise RuntimeError("boom")
        _lb.upload_files = fake_upload2
        notes2 = {"r1": "note2-A", "r2": "note2-B", "r3": "note2-C"}
        done_ids2 = set()
        try:
            upload_rows(rows, files, notes2, False, done_ids2)
            raise AssertionError("phải ném StepError")
        except StepError as e:
            assert e.kind == "up" and "r2" in str(e) and "boom" in str(e)
        assert done_ids2 == {"r1"}
        # #3 Ctrl+C/terminal đóng giữa lúc up: vẫn dọn file dở (remove_files), rồi ném tiếp
        # KeyboardInterrupt; note của dòng đã up trước đó (r1) vẫn phải được ghi.
        def fake_upload3(rid, fs):
            if rid == "r2":
                raise KeyboardInterrupt
        _lb.upload_files = fake_upload3
        notes3 = {"r1": "note3-A", "r2": "note3-B", "r3": "note3-C"}
        calls["remove"] = []; calls["notes"] = None
        done_ids3 = set()
        try:
            upload_rows(rows, files, notes3, False, done_ids3)
            raise AssertionError("KeyboardInterrupt phải ném tiếp")
        except KeyboardInterrupt:
            pass
        assert calls["remove"] == [("r2", ["tok_r2"])]     # cleanup vẫn chạy
        assert calls["notes"] == {"r1": "note3-A"}          # note của r1 (đã up trước) vẫn được ghi
        assert done_ids3 == {"r1"}
    finally:
        _lb.get_row, _lb.upload_files, _lb.remove_files, _lb.write_notes = orig
    # retry cùng ngày: xoá log cũ -> không đọc nhầm error từ lần trước
    logdir = td / "retry_check"; logdir.mkdir(exist_ok=True)
    logfile = logdir / "retry.log"
    # viết log cũ với error (giả sử lần chạy trước lỗi 429)
    logfile.write_text("[gen] ✗ mảnh ao_sau lỗi: HTTP 429 usage_limit\n[gen] ✗ mảnh tay_truoc lỗi: HTTP 429 usage_limit\n")
    assert logfile.exists() and "429" in logfile.read_text(), "test setup: log cũ phải tồn tại"
    # run_pipe gọi: phải xoá log cũ, chạy pipeline, chỉ đọc log mới
    # --selfcheck chạy offline, exit 0, không ghi log -> final log rỗng (hoặc chỉ có output của selfcheck)
    run_pipe(["--selfcheck"], logfile, 60)
    # sau khi chạy xong: log cũ đã bị xoá (bởi run_pipe), và subprocess ghi output mới vào nó
    # selfcheck không ghi lỗi nên log mới sẽ rỗng hoặc chỉ có nội dung selfcheck
    new_log = logfile.read_text() if logfile.exists() else ""
    # lỗi cũ "429" không nên xuất hiện trong log sau khi chạy
    assert "usage_limit" not in new_log, f"log phải được refresh, không còn lỗi cũ. log mới:\n{new_log}"
    # #5 _parse_args: validate --date/--until TRƯỚC acquire_lock; sai -> SystemExit (ap.error)
    import contextlib
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            _parse_args(["--date", "2026-13-40"]); raise AssertionError("--date sai phải lỗi")
        except SystemExit:
            pass
        try:
            _parse_args(["--until", "25:99"]); raise AssertionError("--until giờ sai phải lỗi")
        except SystemExit:
            pass
        try:
            _parse_args(["--until", "7:5"]); raise AssertionError("--until thiếu số 0 phải lỗi")
        except SystemExit:
            pass
        a = _parse_args(["--until", "07:00", "--date", "2026-10-15", "--limit", "3"])
    assert a.until == "07:00" and a.date == "2026-10-15" and a.limit == 3
    # log() với terminal đóng (SIGHUP): print() gây OSError(5, "EIO") nhưng đừng crash main() finally
    class FakeBrokenStdout:
        def write(self, s): raise OSError(5, "EIO")
        def flush(self): raise OSError(5, "EIO")
    old_stdout = sys.stdout
    try:
        sys.stdout = FakeBrokenStdout()
        log("test", "msg")  # phải không ném lỗi
    finally:
        sys.stdout = old_stdout
    print("batch selfcheck OK")


if __name__ == "__main__":
    main()
