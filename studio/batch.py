#!/usr/bin/env python3
"""Flat Studio batch — chạy qua đêm, không giao diện.

  python batch.py [--until 07:00] [--limit N] [--dry-run]
  python batch.py --selfcheck

Lark Base (view 1. DS Order) -> dòng No File chưa có Flat Pieces -> tải Link 1..N -> Claude chọn ảnh
-> flat_pipeline (pieces 3D + art) -> Claude QA, gen lại -> up Flat Pieces + ghi Flat Note.
Spec: docs/superpowers/specs/2026-10-15-batch-lark-flat-design.md
"""
import argparse, hashlib, html, io, json, os, re, shutil, signal, subprocess, sys, threading, time, traceback
import urllib.parse, urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from itertools import count
from pathlib import Path

from PIL import Image

import judge
import lark_base as lb
import nonclothing
from batch_errors import AUTH_HARD_RE, AUTH_RE, QUOTA_RE, AuthError, QuotaError, StepError, raise_for
from flat_pipeline import _pieces_plan, _filter_by_garment, PIECE_BOX, CODEX_WORKERS

ROOT = Path(__file__).resolve().parent
DATA = Path(os.getenv("STUDIO_DATA", ROOT.parent / "flat_studio_data")).expanduser()
BATCH = DATA / "batch"
LOCK = DATA / "batch.lock"
PIPELINE = ROOT / "flat_pipeline.py"
GROUP_TIMEOUT = 60 * 60     # 3D + bản phẳng
QA_WORKERS = 3              # QA từng mảnh: số lệnh claude -p chạy song song
QUOTA_WAIT, QUOTA_TRIES = 30 * 60, 3
UA = {"User-Agent": "Mozilla/5.0"}
_LOGF = None


MAX_ATTEMPTS = 3         # gen lại: lần 1 y nguyên, lần 2-3 có hint + chọn lại ảnh/crop
HINT_FROM = 2            # lần bắt đầu dùng hint (lần y nguyên thứ 2 gần như không cứu được mảnh nào)
SAME_ERR_STOP = 5        # 5 nhóm liên tiếp lỗi cùng kiểu -> dừng đợt
_SIZE = re.compile(r"^(XXS|XS|S|M|L|XL|XXL|XXXL|\d{1,2}XL|\d{1,2}T|\d{1,2}Y|\d{1,2}-\d{1,2}Y?"
                   r"|one ?size|free ?size|\d+(\.\d+)?\s*(x\s*\d+(\.\d+)?)?\s*(in|inch|cm)?)$", re.I)
_KID_RE = re.compile(r"\b(\d{1,2}T|Youth|Kids?|Toddler)\b", re.I)


def _is_kid(sku):
    """SKU Design có size/từ khoá trẻ em (4T, Youth M, Kid Tennis, Toddler...) -> True. Dùng để
    judge.locate biết chọn áo người lớn hay trẻ em khi 1 ô thiết kế có cả 2 (vd polo + áo trẻ em)."""
    return bool(_KID_RE.search(sku or ""))


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
LOI_MAX = 2     # dòng LỖI cùng ngày: thử tối đa 2 lượt rồi thôi (cron chạy mỗi giờ -> khỏi đốt hạn mức)
_LOI_N = re.compile(r"\(lần (\d+)\)\s*$")


def note_loi(day, kind, prev=""):
    """Note LỖI kèm số lượt thử trong ngày: 'LỖI <day>: <kind> (lần n)' (n tăng nếu note cũ cùng ngày)."""
    n = 1
    if (prev or "").startswith(f"LỖI {day}"):
        m = _LOI_N.search(prev)
        n = (int(m.group(1)) if m else 1) + 1
    return f"LỖI {day}: {kind} (lần {n})"


def needs_work(row, day=None):
    """Dòng (đã qua view No File) có gắn tag GEN, chưa có ảnh Flat Pieces, có link. Dòng note SKIP cũ
    (SP không phải quần áo, trước đây bỏ qua) nay vẫn làm: đi nhánh art phẳng (nonclothing.py)."""
    if day and row["note"].startswith(f"LỖI {day}"):
        m = _LOI_N.search(row["note"])
        if (int(m.group(1)) if m else 1) >= LOI_MAX:
            return False
    return row.get("gen", False) and not row["has_pieces"] and bool(row["links"])


def build_queue(rows, day):
    """Gom dòng cần làm theo bộ link; mới nhất lên trước. Lọc ngày đã do view/filter của Base làm
    (lb._query) -> không lọc lại theo Create Date (view No File có thể chứa dòng tạo hôm trước)."""
    groups = {}
    for r in rows:
        if needs_work(r, day):
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
      regen_fn(todo, attempt, hint)                gen lại (attempt 1..MAX_ATTEMPTS; hint từ lần HINT_FROM)
      rehint_fn({name: errors}) -> hint            gọi 1 lần trước lần HINT_FROM (viết hint + chọn lại ảnh/crop)
      keep_fn(name, attempt)                       lưu bản hiện tại để chọn bản tốt nhất
    -> best {name: (attempt, score)}, fails {name: errors} (vẫn lỗi), regen {name: số lần gen lại}."""
    best, last, regen = {}, {}, {}
    todo, hint = list(names), None
    for attempt in range(MAX_ATTEMPTS + 1):
        if attempt:
            if attempt == HINT_FROM:
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
    """Tạo lock NGUYÊN TỬ bằng os.open(O_CREAT|O_EXCL): 2 tiến trình không thể cùng "thấy chưa có
    lock" rồi cùng ghi đè (TOCTOU) — kernel chỉ cho ĐÚNG 1 lệnh open thành công. Lock đã có sẵn ->
    đọc PID cũ: còn sống thì chặn, chết thì gỡ rồi thử tạo lại đúng 1 lần."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                pid = int(path.read_text().strip() or 0)
            except (ValueError, OSError):
                pid = 0
            if pid == os.getpid():
                sys.exit(f"Lock {path} đã thuộc tiến trình này.")
            if pid and _alive(pid):
                sys.exit(f"Đang có batch khác chạy (PID {pid}). Chắc chắn không còn thì xoá {path}.")
            if attempt == 0:
                path.unlink(missing_ok=True)      # PID chết (hoặc file hỏng) -> gỡ rồi thử lại 1 lần
                continue
            sys.exit(f"Không tạo được lock {path} (đua với tiến trình khác).")
        else:
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            return


def release_lock(path=LOCK):
    """Chỉ gỡ lock nếu nó đang chứa PID CỦA MÌNH — tránh gỡ nhầm lock của 1 tiến trình batch khác."""
    try:
        pid = int(path.read_text().strip() or 0)
    except (ValueError, OSError):
        return
    if pid == os.getpid():
        path.unlink(missing_ok=True)


# ---------- ảnh đầu vào ----------
_TAG = re.compile(r"<(?:meta|link)\b[^>]*>", re.I)
_ATTR = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")


def _og_image(page, base):
    """URL ảnh thật trong trang xem ảnh (og:image / image_src), đã giải entity + nối URL tương đối; không có -> None."""
    for tag in _TAG.findall(page[:500_000].decode("utf-8", "replace")):
        a = {k.lower(): v1 or v2 for k, v1, v2 in _ATTR.findall(tag)}
        real = (a.get("content") if a.get("property", "").lower() == "og:image"
                else a.get("href") if a.get("rel", "").lower() == "image_src" else None)
        if real:
            return urllib.parse.urljoin(base, html.unescape(real))
    return None


def download(url, dst, tries=3, hops=1):
    """Tải ảnh, thử lại `tries` lần. Link là TRANG xem ảnh (freeimage.host/i/..., imgur...) thay vì file ảnh ->
    tải ảnh thật ở og:image, tối đa `hops` bước (chặn vòng trang A->B->A) (34-NVH 3D Hoodie 20/10)."""
    last = nxt = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
                data, final = r.read(40 * 1024 * 1024), r.geturl()
            try:
                Image.open(io.BytesIO(data)).verify()
            except Exception:
                nxt = _og_image(data, final) if hops > 0 else None
                if not nxt:
                    raise
                break                       # trang HTML -> tải ảnh thật bên dưới, ngoài vòng thử lại
            dst.write_bytes(data)
            return dst
        except Exception as e:
            last = e
            time.sleep(2 * (i + 1))
    else:
        raise StepError("tải ảnh", f"{url[:80]}: {last}")
    return download(nxt, dst, tries, hops - 1)


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
_ACTIVE = set()             # Popen đang chạy (pipeline con) -> SIGTERM/SIGHUP/Ctrl+C diệt được hết
_ACTIVE_LOCK = threading.Lock()


def _kill_active():
    """Diệt CẢ process group của mọi pipeline đang chạy (không chỉ tiến trình Python cha — Codex/
    OpenArt là cháu, subprocess.run thường chỉ kill được con trực tiếp). Gọi khi dừng giữa chừng
    (SIGTERM/SIGHUP/Ctrl+C) để không phải đợi hết 45' timeout của nhóm."""
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE)
    for p in procs:
        try:
            if sys.platform == "win32":
                p.kill()
            else:
                os.killpg(p.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass          # đã chết sẵn


def run_pipe(args, logf, timeout, cmd=None):
    """Chạy flat_pipeline --single trong process group RIÊNG (start_new_session) -> quá giờ diệt được
    cả tiến trình cháu (Codex/OpenArt), không chỉ tiến trình con trực tiếp. Hết đăng nhập (trong
    log/stderr) -> AuthError; exit≠0 -> raise_for. `cmd` (nội bộ, cho selfcheck) ghi đè lệnh chạy."""
    logf.unlink(missing_ok=True)   # pipeline ghi log kiểu append -> chỉ đọc log của lần chạy này
    # sàn 60s chỉ áp cho lệnh pipeline THẬT (tránh timeout quá ngắn do sai sót); lệnh test (`cmd`
    # truyền thẳng, nội bộ selfcheck) dùng đúng `timeout` được truyền, không bị ép sàn.
    eff_timeout = timeout if cmd else max(60, timeout)
    cmd = cmd or [sys.executable, str(PIPELINE), "--single", *map(str, args), "--log", str(logf)]
    kw = dict(cwd=str(ROOT), text=True, encoding="utf-8", errors="replace",
              env={**os.environ, "PYTHONUTF8": "1"})
    if sys.platform != "win32":
        kw["start_new_session"] = True     # process group riêng -> killpg diệt được cả cháu
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    with _ACTIVE_LOCK:
        _ACTIVE.add(p)
    try:
        try:
            _, stderr = p.communicate(timeout=eff_timeout)
        except subprocess.TimeoutExpired:
            try:
                if sys.platform == "win32":
                    p.kill()
                else:
                    os.killpg(p.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            p.communicate()        # reap, tránh zombie
            raise StepError("quá thời gian", "pipeline")
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(p)
    logtxt = logf.read_text(encoding="utf-8", errors="replace") if logf.exists() else ""
    if _log_auth_error(logtxt, stderr):
        raise AuthError("codex/ARK hết đăng nhập — xem " + str(logf))
    if _piece_quota(logtxt):
        raise QuotaError("hết hạn mức gen giữa chừng — xem " + str(logf))
    if p.returncode != 0:
        raise_for((stderr or "")[-2000:], "gen")
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


def _run_both(fns):
    """Chạy các callable không đối số đồng thời (vd gen pieces + gen art, mỗi cái 1 luồng codex).
    Đợi TẤT CẢ chạy xong rồi mới raise lỗi ĐẦU TIÊN theo thứ tự `fns` — để AuthError/QuotaError/
    StepError vẫn nổi lên như chạy tuần tự, không bỏ dở nhánh còn lại."""
    with ThreadPoolExecutor(max_workers=len(fns)) as ex:
        futs = [ex.submit(fn) for fn in fns]
        errs = []
        for f in futs:
            try:
                f.result()
            except Exception as e:
                errs.append(e)
    if errs:
        # AuthError/QuotaError (dừng cả đợt / chờ rồi thử lại) không được để lỗi nhánh kia (vd
        # timeout StepError của nhánh pieces) che mất -> ưu tiên nổi lên trước, theo thứ tự nặng dần.
        auth = next((e for e in errs if isinstance(e, AuthError)), None)
        quota = next((e for e in errs if isinstance(e, QuotaError)), None)
        raise auth or quota or errs[0]


def _pieces_json_slugs(path, garment=None):
    """pieces.json -> tập slug mảnh (không gồm 'art') theo plan hiện tại (flat_pipeline._pieces_plan),
    lọc theo `garment` (SP chỉ có áo/quần, từ judge.pick) nếu truyền — phòng vision vẫn trả thừa phần."""
    slugs = {p["slug"] for p in _pieces_plan(json.loads(Path(path).read_text(encoding="utf-8")))}
    return _filter_by_garment(slugs, garment) if garment else slugs


def _slugs_match(pieces_json_path, names, garment=None):
    """True nếu bộ mảnh trong pieces.json (bỏ 'art', lọc theo garment) khớp đúng tập `names` (cũng bỏ
    'art'). Dùng ở lần gen lại 3: vision lại có thể đổi loại đồ/tay giống-khác -> đổi tên mảnh."""
    return _pieces_json_slugs(pieces_json_path, garment) == set(names)


def _rehint_target(group, r):
    """Chọn front/crop dùng SAU rehint. mode "combined": ảnh ghép nhiều biến thể/SP trên 1 tấm ->
    front mới rehint trả về có thể LẠC sang biến thể khác -> GIỮ NGUYÊN front+crop của nhóm. Mode
    khác: dùng front/crop rehint trả về (rơi về front/None cũ nếu rehint không chọn). Rehint chọn
    đúng ẢNH SAU của nhóm làm front (hay gặp khi mảnh lỗi là mảnh lưng) -> giữ front cũ, vì vision lại
    với front = ảnh lưng sẽ tả sai mọi mảnh trước (lỗi thật: Tennis Hulk 16/10). Nhóm ĐÃ có crop (định
    vị biến thể / pick) -> giữ nguyên: rehint trả crop null = cả ảnh nhiều design -> vision tả nhầm
    design khác (lỗi thật: Design 2 bị tả thành Design 3, 16/10)."""
    if (group["mode"] == "combined" or group.get("crop")
            or (r["front"] and r["front"] == group.get("back"))):
        return group["front"], group["crop"]
    return r["front"] or group["front"], r["crop"]


# ---------- 1 nhóm con (biến thể thiết kế + màu) ----------
FLAT_FROM_3D = (
    "The reference image is ONE finished garment print panel whose graphics have faux-3D shading. Redraw "
    "EXACTLY the same panel — same layout, shapes, proportions, every graphic and colour — as clean 2D FLAT "
    "vector art: SOLID FLAT COLOR FILL, crisp clean edges, NO 3D, NO shading, NO gradient, NO highlights, NO "
    "glow, NO fabric texture, NO folds. Do NOT add any graphic that is not in the reference. ")
FLAT_BLEED = "Fill the whole frame edge to edge like the reference: NO margin, NO border, NO garment outline."
FLAT_DRESS = "Keep the panel's exact cut shape, on a plain flat light grey background and NOTHING else."
FLAT_ART = (
    "The reference image is a technical drawing of a whole outfit. Redraw EXACTLY the same drawing — same "
    "garments, layout, proportions, every graphic and colour, on the same plain light grey background — as "
    "clean 2D FLAT vector art: SOLID FLAT COLOR FILL, crisp clean outlines, NO 3D, NO shading, NO gradient, "
    "NO highlights, NO fabric texture, NO folds. Do NOT add anything that is not in the reference. ")


def fp_cutout(path):
    from flat_pipeline import cutout
    return cutout(path)


def process_subgroup(group, rows, pick, srcs, sdir, day, dry, deadline, done_ids, only=None, prev=None,
                     replace=False):
    """Gen pieces 3D + art, rồi bản PHẲNG của từng mảnh (USER: clothing xuất cả 2 bản, mỗi bản QA riêng) cho
    1 nhóm con (biến thể + màu) -> QA/gen lại -> up. Trả True nếu còn mảnh QA FAIL.
    only + prev (--fix-fails): CHỈ gen lại các mảnh trong `only` (vd {'ao_sau', 'ta_truoc_phang'}) với mô tả /
    hint của lần chạy trước ở thư mục `prev` (bỏ vision); bản phẳng của mảnh 3D không gen lại thì vẽ từ file
    3D cũ trong prev/out; up xong chỉ thay đúng các file đó trên Base (mảnh đã đạt giữ nguyên)."""
    shutil.rmtree(sdir, ignore_errors=True)    # thử lại cùng ngày: bỏ log/mảnh/pieces.json của lần trước
    sdir.mkdir(parents=True)
    old_hint = None
    if prev:
        shutil.copyfile(prev / "pieces.json", sdir / "pieces.json")
        if (prev / "rehint.json").exists():   # lần trước đã đổi ảnh/crop + hint -> dùng lại đúng bối cảnh đó
            r = json.loads((prev / "rehint.json").read_text(encoding="utf-8"))
            group = {**group}
            group["front"], group["crop"] = _rehint_target(group, r)
            old_hint = r.get("hint") or None
    color = None if group["matches_photo"] else group["color"]
    colour_desc = color or "same as the reference photo"
    back_crop = group.get("back_crop")
    ctx = {"front": make_input(srcs[group["front"] - 1], group["crop"], sdir / "in_front.png"),
           "front_src": srcs[group["front"] - 1],        # ảnh gốc chưa cắt, để QA luôn so với ảnh thật
           "cropped": group["crop"] is not None,
           "back": (make_input(srcs[group["back"] - 1], back_crop, sdir / "in_back.png")
                    if group["mode"] == "two" else None),
           "back_cropped": group["mode"] == "two" and back_crop is not None,
           "hint": old_hint}
    n = Counter()
    pipe_no = count(1)   # pieces + art gen SONG SONG -> itertools.count thread-safe, không trùng tên log

    def pipe(args):
        left = deadline - time.monotonic()
        if left <= 0:
            raise StepError("quá thời gian", "nhóm > 45 phút")
        extra = ((["--color", color] if color else []) + (["--hint", ctx["hint"]] if ctx["hint"] else [])
                 + ["--garment", group["garment"]])
        return run_pipe(args + extra, sdir / f"pipe_{next(pipe_no):02d}.log", left)

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

    pj = sdir / "pieces.json"
    if only:
        # chỉ mảnh 3D lỗi (+ art nếu lỗi), mô tả lấy từ pieces.json cũ (--cache, bỏ vision)
        names = sorted(x for x in only if not x.endswith("_phang"))
        slugs = [x for x in names if x != "art"]
        jobs = ([lambda: gen_pieces(only=slugs, cache=True)] if slugs else []) + ([gen_art] if "art" in names else [])
        if jobs:
            _run_both(jobs)
    else:
        # lần 0: pieces (GEN_WORKERS mảnh/luồng) + art chạy SONG SONG -> tổng CODEX_WORKERS luồng codex
        _run_both([gen_pieces, gen_art])
        if not pj.exists():
            raise StepError("gen", "vision không ra pieces.json")
        want = _pieces_json_slugs(pj, group["garment"])
        have = {p.stem[len("final_"):] for p in sdir.glob("final_*.png")}
        names = sorted(want | have)          # mảnh lỗi lần 0 (thiếu file) vẫn vào danh sách -> QA chấm 0 -> gen lại
        if not names:
            raise StepError("gen", "không ra mảnh nào")
        names.append("art")                 # art lỗi lần 0 -> QA chấm 0 -> được gen lại

    def make_qa(piece_path, flat=False):
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
                    + ([(srcs[group["back"] - 1], "REF back photo")] if ctx["back_cropped"] else [])
                    + ([(ctx["back"], "REF back")] if ctx["back"] else []))
            k = n["qa"]

            def one(x):   # chấm TỪNG mảnh, ảnh lớn + tiêu chí riêng (ảnh ghép chung nhỏ -> QA lọt lỗi dáng)
                try:
                    return x, judge.qa_piece(refs, piece_path(x), x, colour_desc, flat,
                                             sdir / f"sheet_qa_{k}_{x}.jpg")
                except StepError as e:
                    log("qa", f"{x}: QA lỗi {e}")
                    return x, {"ok": False, "score": 0.0, "errors": []}
            with ThreadPoolExecutor(max_workers=QA_WORKERS) as ex:
                res.update(dict(ex.map(one, have)))
        (sdir / f"qa_{n['qa']}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        log("qa", " ".join(f"{k}:{'✓' if v['ok'] else '✗'}{v['score']:.0f}" for k, v in res.items()))
        return res
      return qa_fn

    def claude_boxes(slugs):
        """Claude khoanh box mảnh trên ảnh đầu vào hiện tại (lưới pixel) -> ghi đè boxes trong pieces.json.
        Mặt sau tự suy (không ảnh lưng) bỏ qua: không có vùng thật để cắt."""
        if not pj.exists():
            return
        js = json.loads(pj.read_text(encoding="utf-8"))
        if not isinstance(js.get("boxes"), dict):
            js["boxes"] = {}
        for slug in slugs:
            if slug not in PIECE_BOX or (slug.endswith("_sau") and group["mode"] == "infer"):
                continue
            key, label = PIECE_BOX[slug]
            back = slug.endswith("_sau")
            img = ctx["back"] if back and ctx["back"] else ctx["front"]
            try:
                box = judge.locate_piece(judge.grid_overlay(img, sdir / f"grid_{slug}.jpg"), label, back)
            except StepError as e:
                log("regen", f"Claude khoanh {slug} lỗi: {e}"); continue
            log("regen", f"Claude khoanh {slug}: {box}")
            if box:
                js["boxes"][key] = box
        pj.write_text(json.dumps(js, ensure_ascii=False), encoding="utf-8")

    def regen_fn(todo, attempt, hint):
        log("regen", f"lần {attempt}: {', '.join(todo)}" + (" (hint)" if hint else ""))
        for x in todo:
            piece_path(x).unlink(missing_ok=True)
        slugs = [x for x in todo if x != "art"]
        need_art = "art" in todo

        def do_pieces():
            if attempt == HINT_FROM:
                # lần đầu có hint: vision lại (crop + hint mới, từ rehint_fn) -> có thể đổi loại đồ/tay
                # giống-khác -> tên mảnh lệch với bản gốc (USER DECISION: khi lệch thì quay về
                # mô tả cũ, giữ crop + hint mới).
                v0 = sdir / "pieces_v0.json"
                shutil.copyfile(pj, v0)
                gen_pieces(only=slugs, cache=False)
                old = {n for n in names if n != "art"}
                if pj.exists() and not _slugs_match(pj, old, group["garment"]):
                    new = _pieces_json_slugs(pj, group["garment"])
                    log("regen", f"lần {attempt} vision ra mảnh khác ({sorted(new)} != {sorted(old)})"
                                 " -> quay lại mô tả cũ, giữ crop+hint mới")
                    for p in sdir.glob("final_*.png"):
                        if p.stem[len("final_"):] not in old:
                            p.unlink(missing_ok=True)
                    # mô tả v0 + box MỚI: box v0 tính trên ảnh CŨ, vision lần 2 vừa khoanh lại trên ảnh mới
                    js0 = json.loads(v0.read_text(encoding="utf-8"))
                    js0["boxes"] = (json.loads(pj.read_text(encoding="utf-8")).get("boxes") or {})
                    pj.write_text(json.dumps(js0, ensure_ascii=False), encoding="utf-8")
                    missing = [x for x in slugs if not piece_path(x).exists()]
                    if missing:
                        gen_pieces(only=missing, cache=True)
            else:
                if attempt == MAX_ATTEMPTS:
                    claude_boxes(slugs)                # lần cuối: Claude nhìn ảnh, khoanh lại vùng mảnh lỗi
                gen_pieces(only=slugs, cache=True)     # lần 1: y nguyên; lần sau hint: dùng pieces.json hiện tại

        if slugs and need_art:
            _run_both([do_pieces, gen_art])            # song song: mảnh + art = CODEX_WORKERS luồng codex
        elif slugs:
            do_pieces()
        elif need_art:
            gen_art()

    def rehint_fn(fails):
        items = [(p, str(i)) for i, p in enumerate(srcs, 1)]
        items += [(piece_path(x), x) for x in fails if piece_path(x).exists()]
        sheet = judge.contact_sheet(judge.grid(items), sdir / "sheet_rehint.jpg")
        skus = "; ".join(sorted({row["sku"] for row in rows}))
        try:
            r = judge.rehint(sheet, fails, len(srcs), variant=group["variant"], garment=group["garment"],
                             skus=skus)
        except StepError as e:               # judge lỗi -> không phá nhóm, gen lại y nguyên crop cũ
            log("regen", f"rehint lỗi, giữ ảnh/crop cũ: {e}")
            ctx["hint"] = None
            return None
        (sdir / "rehint.json").write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
        # mode "combined": ảnh ghép nhiều biến thể -> GIỮ NGUYÊN front+crop của nhóm (front mới có
        # thể lạc sang biến thể/SP khác trên cùng ảnh ghép); mode khác: dùng front/crop rehint trả về
        front, crop = _rehint_target(group, r)
        front_idx = front - 1
        ctx["front_src"] = srcs[front_idx]
        ctx["front"] = make_input(srcs[front_idx], crop, sdir / "in_front_r3.png")
        ctx["cropped"] = crop is not None
        ctx["hint"] = r["hint"] or None
        log("regen", f"hint: {ctx['hint']}")
        return ctx["hint"]

    def make_keep(path):
        def keep_fn(name, attempt):
            src = path(name)
            if src.exists():
                (sdir / "attempts").mkdir(exist_ok=True)
                shutil.copyfile(src, sdir / "attempts" / f"{name}_{attempt}.png")
        return keep_fn

    best, fails, regen = (improve(names, make_qa(piece_path), regen_fn, rehint_fn, make_keep(piece_path))
                          if names else ({}, {}, {}))

    # ---- bản PHẲNG: vẽ lại TỪ mảnh 3D đã chốt (ref = đúng mảnh đó) -> không lấn họa tiết mảnh khác (vẽ từ
    # ảnh gốc thì tay phẳng dính sọc thân/quần, Jogger 19/10); art dùng chung ----
    fdir = sdir / "flat"; fdir.mkdir(exist_ok=True)
    flat_names = ([x for x in sorted(only) if x.endswith("_phang")] if only
                  else [f"{x}_phang" for x in names])     # gồm cả art_phang (USER: bộ phẳng đủ như Jogger D5)

    def flat_path(name):
        return fdir / f"final_{name[:-len('_phang')]}.png"

    def gen_flat_one(name):
        base = name[:-len("_phang")]
        if base in best:
            ref = sdir / "attempts" / f"{base}_{best[base][0]}.png"
        else:                           # --fix-fails: mảnh 3D không gen lại -> vẽ từ file 3D đã up lần trước
            ref = next((prev / "out" / piece_filename(rows[0]["sku"], base, failed=f) for f in (False, True)
                        if prev and (prev / "out" / piece_filename(rows[0]["sku"], base, failed=f)).exists()),
                       sdir / "missing")
        if not ref.exists():
            return
        dress = base.startswith(("than_", "ta_"))       # mảnh đầm cắt cong: vẽ trên nền xám rồi cutout
        prompt = (FLAT_ART if base == "art" else FLAT_FROM_3D + (FLAT_DRESS if dress else FLAT_BLEED)) + (
            f" FIX: {ctx['hint']}" if ctx["hint"] else "")
        try:
            nonclothing._gen(prompt, ref, flat_path(name))
            if dress:
                fp_cutout(flat_path(name)).save(flat_path(name))
        except StepError as e:
            if e.kind != "gen":
                raise
            log("gen", f"{name}: lỗi gen (tính 1 lần hỏng): {e}")

    def gen_flat(todo):
        if time.monotonic() > deadline:
            raise StepError("quá thời gian", "nhóm > 60 phút")
        with ThreadPoolExecutor(max_workers=CODEX_WORKERS) as ex:
            list(ex.map(gen_flat_one, todo))

    def regen_flat(todo, attempt, hint):
        log("regen", f"phẳng lần {attempt}: {', '.join(todo)}")
        for x in todo:
            flat_path(x).unlink(missing_ok=True)
        gen_flat(todo)

    gen_flat(flat_names)
    fbest, ffails, fregen = improve(flat_names, make_qa(flat_path, flat=True), regen_flat,
                                    lambda fails_: None, make_keep(flat_path))
    best.update(fbest); regen.update(fregen); fails = {**fails, **ffails}

    out = sdir / "out"; out.mkdir(exist_ok=True)
    files = []
    for name in names + flat_names:
        src = sdir / "attempts" / f"{name}_{best[name][0]}.png"
        if src.exists():                    # mảnh chưa từng ra file thì không có gì để up
            dst = out / piece_filename(rows[0]["sku"], name, failed=name in fails)
            shutil.copyfile(src, dst); files.append(dst)
    if not files:
        raise StepError("gen", "không có mảnh nào để up")
    n_pieces = sum(1 for x in names if x != "art")
    if only:
        note = (note_fail(day, fails) if fails else f"OK {day}") + " · gen lại riêng mảnh lỗi: " + ", ".join(sorted(only))
    else:
        note = note_fail(day, fails) if fails else note_ok(day, n_pieces, regen) + " · + bản phẳng (mảnh + art)"
    manual = pick.get("manual") or {}
    notes = {r["record_id"]: note + (f" · CẦN CHỈNH: {manual[r['record_id']]}"
                                     if r["record_id"] in manual else "") for r in rows}
    if replace or only:   # --regen / --fix-fails: chỉ thay mảnh CÓ file mới (up trước, gỡ sau); mảnh gen hỏng hẳn
        # giữ nguyên bản cũ trên Base, không gỡ mất (codex review 20/10)
        replace_rows(rows, files, {piece_slug(rows[0]["sku"], f.name)[0] for f in files}, notes, dry)
    else:
        upload_rows(rows, files, notes, dry, done_ids)
    if not dry:
        (sdir / "uploaded.ok").write_text(datetime.now().isoformat(), encoding="utf-8")   # _find_prev: đã lên Base
    return bool(fails)


def piece_slug(sku, filename):
    """Tên file mảnh trên Base -> (slug, đã QAFAIL?) theo piece_filename; không phải file mảnh -> (None, False)."""
    prefix, stem = f"{safe_name(sku_stem(sku))}_", Path(filename).stem
    if not stem.startswith(prefix):
        return None, False
    slug = stem[len(prefix):]
    failed = slug.endswith("_QAFAIL")
    return (slug[:-len("_QAFAIL")] if failed else slug), failed


def replace_rows(rows, files, names, notes, dry):
    """--fix-fails / --regen: up file mới TRƯỚC, rồi mới gỡ file cũ của đúng các mảnh `names` (đạt hay QAFAIL;
    None = gỡ TOÀN BỘ file cũ) — up lỗi thì Base vẫn còn bản cũ, không mất mảnh nào (lỗi thật 20/10: gỡ trước
    up sau, mạng đứt giữa chừng -> 4205299353 còn 0 file)."""
    if dry:
        log("dry", f"[không ghi Base] thay {sorted(names) if names is not None else 'TOÀN BỘ'} · note: {notes}")
        return
    for r in rows:
        rid = r["record_id"]
        old = [p["file_token"] for p in lb.get_row(rid)["pieces"]
               if names is None or piece_slug(r["sku"], p.get("name", ""))[0] in names]
        lb.upload_files(rid, files)
        if old:
            lb.remove_files(rid, old)
        lb.write_notes({rid: notes[rid]})
        log("up", f"{rid} ✓ thay {len(files)} file (gỡ {len(old)} cũ)")


def _find_prev(rid):
    """Lần chạy gần nhất có dòng `rid`: (gdir, k, group, pick) — group thứ k (c<k>) có pieces.json; không có -> None."""
    best = None
    for pj in BATCH.glob("*/*/pick.json"):
        try:
            pick = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for k, g in enumerate(pick.get("groups") or [], 1):
            c = pj.parent / f"c{k}"
            # lần chạy đã up xong (out/ có file) — bỏ qua lần dừng giữa chừng / chưa up
            if rid in g.get("record_ids", []) and (c / "pieces.json").exists() and any(c.glob("out/*.png")):
                # ưu tiên lần chạy ĐÃ up xong (uploaded.ok); lần chạy cũ (trước khi có dấu) xếp sau
                key = ((c / "uploaded.ok").exists(), pj.stat().st_mtime)
                if not best or key > best[0]:
                    best = (key, pj.parent, k, g, pick)
    return best and best[1:]


def fix_fails(keys, day, dry):
    """--fix-fails: với mỗi Order ID / record_id -> gen lại CHỈ các mảnh đang _QAFAIL trên Base."""
    for key in keys:
        for row in lb.find_rows(key):
            rid = row["record_id"]
            failed = {sl for sl, bad in (piece_slug(row["sku"], p.get("name", "")) for p in row["pieces"]) if bad}
            if not failed:
                log("fix", f"{key} {rid}: không có mảnh _QAFAIL -> bỏ qua"); continue
            found = _find_prev(rid)
            if not found:
                log("fix", f"{key} {rid}: không thấy lần chạy trước (pick.json + pieces.json) -> bỏ qua"); continue
            pgdir, k, group, pick = found
            log("group", f"=== FIX {key} {rid} · {row['sku']} · mảnh lỗi: {', '.join(sorted(failed))} · từ {pgdir} ===")
            gdir = BATCH / day / f"{pgdir.name.split('_fix')[0]}_fix_{rid[-6:]}"; gdir.mkdir(parents=True, exist_ok=True)
            srcs = [download(u, gdir / f"src_{i:02d}.jpg") for i, u in enumerate(row["links"], 1)]
            sdir, psdir = gdir / f"c{k}", pgdir / f"c{k}"
            try:
                process_subgroup(group, [row], pick, srcs, sdir, day, dry,
                                 time.monotonic() + GROUP_TIMEOUT, set(), only=failed, prev=psdir)
            except StepError as e:
                log("lỗi", f"FIX {key}: {e}")
                continue
            if dry:
                continue
            # lần fix sau phải lấy ĐÚNG bản hiện có trên Base: out/ = file mới + file cũ không thay; ghi pick.json
            # SAU khi up xong (_find_prev chỉ coi lần chạy này là nguồn khi đã hoàn tất)
            done = {piece_slug(row["sku"], f.name)[0] for f in (sdir / "out").glob("*.png")}
            for f in psdir.glob("out/*.png"):
                if piece_slug(row["sku"], f.name)[0] not in done:
                    shutil.copyfile(f, sdir / "out" / f.name)
            for name in ("rehint.json",):
                if (psdir / name).exists() and not (sdir / name).exists():
                    shutil.copyfile(psdir / name, sdir / name)
            (gdir / "pick.json").write_text(json.dumps(pick, ensure_ascii=False, indent=1), encoding="utf-8")
            cleanup(gdir)


def regen(keys, day, dry, stats):
    """--regen: gen lại TOÀN BỘ các Order ID / record_id chỉ định (kể cả dòng đã có file / không tag GEN) qua
    đúng luồng batch; up xong mới thay toàn bộ file cũ."""
    for key in keys:
        for row in lb.find_rows(key):
            if not row["links"]:
                log("regen", f"{key} {row['record_id']}: không có link ảnh -> bỏ qua"); continue
            g = build_queue([dict(row, has_pieces=False, gen=True, note="")], day)[0]
            log("regen", f"{key} {row['record_id']} · {row['sku']}")
            process_group(g, BATCH / day, day, dry, stats, replace=True)


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
                # dòng đã có file từ trước (vd gen lại đè) -> coi là DONE, để 1 dòng khác lỗi
                # quota/StepError sau đó trong CÙNG nhóm không ghi LỖI đè lên dòng đã xong này
                log("up", f"{rid} đã có Flat Pieces -> bỏ qua"); done_ids.add(rid); continue
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
            except AuthError as e:
                # hết đăng nhập ngay lúc ghi note -> KHÔNG được nuốt: dòng đã up file nhưng Note
                # trống/cũ nếu bỏ qua, và đợt phải dừng ngay (như mọi AuthError khác) chứ không
                # âm thầm tiếp tục nhóm sau.
                log("up", f"ghi note up thất bại (hết đăng nhập): {e}")
                raise
            except Exception as e:
                log("up", f"ghi note up thất bại: {e}")


def _needs_locate(group, front_counts, custom=""):
    """Nhóm cần định vị lại bằng ảnh lớn + lưới toạ độ: front dùng chung với nhóm khác (nhiều biến
    thể trên 1 ảnh, contact sheet ~384px/tile quá nhỏ để Claude thấy rõ từng ô), hoặc nhóm đã có crop
    (toạ độ đoán từ tile nhỏ -> đáng nghi, định vị lại cho chính xác hơn), hoặc dòng có Name Custom
    (vd 'Design 3': 1 dòng nhưng ảnh có nhiều design đánh số)."""
    return front_counts[group["front"]] > 1 or group["crop"] is not None or bool(custom)


def _variant_label(group, sub_rows):
    """Nhãn biến thể đưa cho locate: variant của judge, hoặc Name Custom khi judge để 'default'."""
    if group["variant"] and group["variant"] != "default":
        return group["variant"]
    return "; ".join(sorted({r["custom"] for r in sub_rows if r.get("custom")})) or group["variant"]


LOCATE_TRIES = 3
_DESIGN_NUM = re.compile(r"design\s*#?\s*(\d+)", re.I)


def _check_design_photos(groups, rows, srcs, sheet, log_=None):
    """Dòng 'Design N': Claude đọc ảnh legend có số (badge 1,2,3...) -> ảnh nào là đúng design N; front
    pick chọn không nằm trong đó -> đổi sang ảnh đúng (pick hay đoán nhầm: Jogger Design 5 lấy bộ ④,
    Jumpsuit Design 1 lấy bộ ④, 16/10). Không có legend / không đọc được -> giữ nguyên pick."""
    want = []
    for g in groups:
        sub = [r for r in rows if r["record_id"] in g["record_ids"]]
        m = _DESIGN_NUM.search(" ".join([g["variant"] or ""] + [r.get("custom") or "" for r in sub]
                                        + [r["sku"] for r in sub]))
        if m:
            want.append((g, int(m.group(1))))
    if not want:
        return
    try:
        legend = judge.find_legend(sheet, len(srcs))
    except StepError as e:
        log("design", f"tìm ảnh legend lỗi: {e}"); return
    if not legend:
        log("design", "không có ảnh legend đánh số -> giữ ảnh pick chọn"); return
    for g, num in want:
        try:
            m = judge.match_design(sheet, srcs[legend - 1], num, len(srcs), g.get("garment") or "outfit")
        except StepError as e:
            log("design", f"Design {num}: đọc legend lỗi, giữ front={g['front']}: {e}"); continue
        photos = [p for p in m["photos"] if p != legend]
        log("design", f"Design {num} = {m['desc']} -> ảnh {photos} (legend {legend}, pick front={g['front']})")
        # không ảnh riêng nào khớp -> dùng chính ảnh legend (có đủ các design, locate cắt đúng Design N); giữ
        # front của pick thì locate tìm 'Design N' trên ảnh 1 design khác -> không thấy (Athletic Set D1 19/10)
        new = photos[0] if photos else legend
        if g["front"] not in photos:
            log("design", f"Design {num}: pick chọn {g['front']} không khớp -> đổi sang {new}"
                          + ("" if photos else " (ảnh legend)"))
            g["front"], g["crop"], g["back"], g["back_crop"] = new, None, None, None
            if g["mode"] == "two":
                g["mode"] = "combined"     # ảnh mới thường có cả trước + sau; locate/vision tự tách
        # ponytail: tin thứ tự judge (ảnh CHỈ có bộ đó xếp đầu); nếu photos[0] lại là ảnh nhóm thì gen cả ảnh
        g["design_photo"] = bool(photos) and g["front"] == photos[0]


def _locate_groups(groups, rows, srcs, gdir):
    """Với mỗi nhóm cần định vị (`_needs_locate`): dựng gdir/grid_<front>.jpg (ảnh gốc + lưới 0-1000,
    dùng chung cho các nhóm cùng front) rồi hỏi Claude khoanh CHÍNH XÁC vùng của biến thể đó -> cập
    nhật group['crop']. Không tìm thấy / judge lỗi -> giữ crop cũ. -> list nhóm KHÔNG thể gen: front
    dùng chung với nhóm khác hoặc dòng có Name Custom, mà vẫn không có crop -> gen cả ảnh sẽ tả nhầm biến thể."""
    failed = []
    front_counts = Counter(g["front"] for g in groups)
    grids = {}
    for group in groups:
        sub_rows = [r for r in rows if r["record_id"] in group["record_ids"]]
        custom = "; ".join(r.get("custom", "") for r in sub_rows if r.get("custom"))
        if not _needs_locate(group, front_counts, custom):
            continue
        front = group["front"]
        if front not in grids:
            grids[front] = judge.grid_overlay(srcs[front - 1], gdir / f"grid_{front}.jpg")
        skus = "; ".join(sorted({r["sku"] for r in sub_rows}))
        kid = any(_is_kid(r["sku"]) for r in sub_rows)
        box = None
        for k in range(1, LOCATE_TRIES + 1):        # Claude nhìn ảnh lớn định vị, tối đa LOCATE_TRIES lần
            try:
                box = judge.locate(grids[front], _variant_label(group, sub_rows), skus, kid)
            except StepError as e:
                log("locate", f"{group['variant']}/{group['color']} lần {k}: lỗi {e}")
            if box:
                break
        if box:
            group["crop"] = box
            log("locate", f"{group['variant']}/{group['color']}: crop={box}")
        else:
            log("locate", f"{group['variant']}/{group['color']}: không định vị được, giữ crop cũ "
                          f"{group['crop']}")
            # ảnh dùng chung nhiều nhóm, HOẶC dòng có Name Custom (vd 'Design 3': có thể chỉ 1 dòng của ảnh
            # nhiều design được gắn GEN, hoặc dòng anh em đã up ở lượt trước) -> gen cả ảnh sẽ lấy nhầm design
            # ngoại lệ: front là ảnh khớp ĐẦU của design (legend, `design_photo`) và không chung nhóm khác ->
            # thường là ảnh chỉ có 1 bộ, locate tìm nhãn 'Design N' không thấy -> gen cả ảnh thay vì loại nhầm
            # (Athletic Set D1 19/10). Vẫn locate trước: nếu đó lại là ảnh nhóm thì locate cắt được đúng design.
            sole = group.get("design_photo") and front_counts[front] == 1
            if group["crop"] is None and (front_counts[front] > 1 or custom) and not sole:
                failed.append(group)
    return failed


# ---------- 1 nhóm ----------
def cleanup(gdir):
    """Sau khi up xong: xoá ảnh tải về + bản trung gian; giữ out/, contact sheet, json, log."""
    for p in list(gdir.glob("src_*")) + list(gdir.glob("c*/attempts")) + list(gdir.glob("c*/art_work")):
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    for p in gdir.glob("c*/*.png"):
        p.unlink(missing_ok=True)


def process_nonclothing(rows, srcs, sheet, gdir, day, dry, stats, done_ids, replace=False):
    """SP không phải quần áo (nonclothing.py): mỗi dòng 2 art (phẳng + giả 3D) -> up + note riêng từng dòng."""
    any_fail = False
    for r, arts in nonclothing.run(rows, srcs, sheet, gdir, log):
        od = gdir / f"nc_{r['record_id']}" / "out"; od.mkdir(parents=True, exist_ok=True)
        files, fails = [], {}
        for v, (art, q) in arts.items():
            if not q["ok"]:
                fails[f"art_{v}"] = q["errors"] or ["điểm thấp"]
            f = od / piece_filename(r["sku"], f"art_{v}", not q["ok"])
            shutil.copyfile(art, f)
            files.append(f)
        any_fail |= bool(fails)
        note = (f"QA FAIL {day} · " + " · ".join(f"{k} ({','.join(e)})" for k, e in fails.items())
                + f" sau {nonclothing.MAX_ATTEMPTS} lần" if fails
                else f"OK {day} · art phẳng + giả 3D (không phải quần áo)")
        if replace:
            replace_rows([r], files, {piece_slug(r["sku"], f.name)[0] for f in files}, {r["record_id"]: note}, dry)
        else:
            upload_rows([r], files, {r["record_id"]: note}, dry, done_ids)
    stats["qafail" if any_fail else "ok"] += 1
    if not dry:
        cleanup(gdir)
    return None


def process_group(g, rundir, day, dry, stats, replace=False):
    """-> None (gen xong), 'skip', hoặc kind của StepError. QuotaError/AuthError ném tiếp lên main."""
    gid = group_id(g)
    gdir = rundir / gid; gdir.mkdir(parents=True, exist_ok=True)
    # done_ids lưu trong g: chờ hạn mức xong main gọi lại nhóm này -> chỉ làm các dòng CHƯA up
    done_ids = g.setdefault("done", set())
    rows, deadline = [r for r in g["rows"] if r["record_id"] not in done_ids], time.monotonic() + GROUP_TIMEOUT
    if not rows:
        return None
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
        if pick["skip"]:                   # KHÔNG phải quần áo -> nhánh riêng, không đụng luồng clothing
            log("nc", f"không phải quần áo ({pick['skip']}) -> art phẳng")
            return process_nonclothing(rows, srcs, sheet, gdir, day, dry, stats, done_ids, replace)
        unassigned = pick.get("unassigned") or []
        if unassigned:
            # Claude bỏ sót / nhóm front sai -> KHÔNG đoán dồn vào nhóm khác (sai biến thể/màu) ->
            # ghi LỖI riêng cho đúng các dòng đó, vẫn xử lý tiếp các nhóm hợp lệ còn lại bên dưới.
            log("lỗi", f"unassigned (judge không xác định được): {unassigned}")
            prev = {r["record_id"]: r.get("note", "") for r in rows}
            if not dry:
                lb.write_notes({rid: note_loi(day, "judge (không xác định phiên bản)", prev.get(rid, ""))
                                for rid in unassigned})
        _check_design_photos(pick["groups"], rows, srcs, sheet)
        for bad in _locate_groups(pick["groups"], rows, srcs, gdir):
            pick["groups"].remove(bad)
            log("lỗi", f"{bad['variant']}: ảnh chung nhiều biến thể, không định vị được -> không gen")
            prev = {r["record_id"]: r.get("note", "") for r in rows}
            if not dry:
                lb.write_notes({rid: note_loi(day, "không định vị được phiên bản", prev.get(rid, ""))
                                for rid in bad["record_ids"]})
        (gdir / "pick.json").write_text(json.dumps(pick, ensure_ascii=False, indent=1), encoding="utf-8")
        log("pick", "; ".join(f"{g['variant']}/{g['color']}: front={g['front']} back={g['back']} "
                              f"mode={g['mode']}" for g in pick["groups"]))
        any_fail = False
        for k, group in enumerate(pick["groups"], 1):
            sub_rows = [r for r in rows if r["record_id"] in group["record_ids"]]
            any_fail |= process_subgroup(group, sub_rows, pick, srcs, gdir / f"c{k}", day, dry, deadline, done_ids,
                                         replace=replace)
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
        left = {r["record_id"]: note_loi(day, kind, r.get("note", "")) for r in rows if r["record_id"] not in done_ids}
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
    ap.add_argument("--fix-fails", nargs="+", metavar="ID",
                    help="chỉ gen lại các mảnh _QAFAIL của các Order ID / record_id này (mảnh đạt giữ nguyên)")
    ap.add_argument("--regen", nargs="+", metavar="ID",
                    help="gen lại TOÀN BỘ các Order ID / record_id này rồi thay hết file cũ (up trước, gỡ sau)")
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
    _kill_active()     # diệt pipeline con ngay (không đợi executor join tiến trình đang subprocess.run)
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
    stats, stop, reason, queue = Counter(), Stopper(a.until, a.limit), "hết việc", []
    log("start", f"date={only_day} until={a.until} limit={a.limit} dry={a.dry_run}"
                 + (f" fix-fails={a.fix_fails}" if a.fix_fails else "") + (f" regen={a.regen}" if a.regen else ""))
    if a.fix_fails or a.regen:               # chế độ riêng: chỉ làm các mẫu chỉ định
        try:
            if a.regen:
                regen(a.regen, day, a.dry_run, stats)
            if a.fix_fails:
                fix_fails(a.fix_fails, day, a.dry_run)
        except (AuthError, QuotaError, KeyboardInterrupt) as e:
            _kill_active(); log("done", f"dừng: {e!r}")
        finally:
            release_lock()
        return
    try:
        queue = build_queue(lb.fetch_rows(only_day, day), only_day)
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
        _kill_active()     # Ctrl+C ở foreground đi qua SIGINT mặc định của Python, KHÔNG qua _raise_kbd
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
            if queue or reason != "hết việc":   # watch_gen.sh chạy mỗi giờ: không có mẫu mới -> không báo Lark
                notify(summary)
        finally:
            release_lock()


def _selfcheck():
    # selfcheck không được gọi claude thật: bước đọc legend mặc định "không có legend"
    judge.find_legend = lambda sheet, n: None
    # tên file: bỏ mã lô + size; ký tự cấm -> _
    assert sku_stem("HKH032604A21-Black-2D Hoodie-L-G130426") == "HKH032604A21-Black-2D Hoodie"
    assert sku_stem("VTY022506A07-Christoph Stelzer1-Button Shirt-2XL-J150426") == \
        "VTY022506A07-Christoph Stelzer1-Button Shirt"
    assert sku_stem("VPC132605A14-Austin 080626-Car visor clip-AP8626") == \
        "VPC132605A14-Austin 080626-Car visor clip"
    assert sku_stem("X") == "X"
    assert piece_filename("A/B-Black-Tee-M-G1", "tay_sau", failed=True) == "A_B-Black-Tee_tay_sau_QAFAIL.png"
    assert piece_filename("A-Tee-S-G1", "art") == "A-Tee_art.png"
    # 1 nhóm, không crop, nhưng dòng có Name Custom 'Design 3' (ảnh nhiều design) -> vẫn định vị
    g1 = {"front": 1, "crop": None, "variant": "default"}
    assert not _needs_locate(g1, Counter([1])) and _needs_locate(g1, Counter([1]), "Design: Design 3")
    assert _variant_label(g1, [{"custom": "Design: Design 3"}]) == "Design: Design 3"
    assert _variant_label({**g1, "variant": "Women"}, [{"custom": "x"}]) == "Women"
    # _is_kid: size/từ khoá trẻ em trong SKU Design -> True (để judge.locate chọn áo người lớn/trẻ em)
    assert _is_kid("HKH032604A21-Black-Kid Tennis Shirt-M-G130426")
    assert _is_kid("HKH032604A21-Black-2D Hoodie-4T-G130426")
    assert _is_kid("HKH032604A21-Black-2D Hoodie-Youth M-G130426")
    assert not _is_kid("HKH032604A21-Black-2D Hoodie-L-G130426")
    assert not _is_kid("HKH032604A21-Black-2D Hoodie-2XL-G130426")
    # hàng đợi: lọc, chỉ đúng ngày `day`, gom theo bộ link, mới nhất trước
    def row(rid, links, created, **kw):
        return {"record_id": rid, "sku": f"S-{rid}-Tee-M-G1", "color": "", "created": created,
                "check_file": kw.get("cf", ["No File"]), "links": links,
                "has_pieces": kw.get("hp", False), "pieces": [], "note": kw.get("note", ""),
                "gen": kw.get("gen", True)}
    rows = [row("a", ["u1"], "2026-04-09T01:00:00.000+07:00"),
            row("b", ["u1"], "2026-04-09T03:00:00.000+07:00"),
            row("c", ["u2"], "2026-04-09T02:00:00.000+07:00"),
            row("d", ["u3"], "2026-04-09T09:00:00.000+07:00", hp=True),
            row("e", ["u4"], "2026-04-09T09:00:00.000+07:00", note="SKIP: mug"),   # SKIP cũ -> vẫn làm (art phẳng)
            row("f", ["u5"], "2026-04-09T09:00:00.000+07:00", gen=False),   # không gắn GEN -> bỏ
            row("g", [], "2026-04-09T09:00:00.000+07:00"),
            row("h", ["u6"], "2026-04-09T09:00:00.000+07:00", note="LỖI 2026-10-15: tải ảnh")]
    q = build_queue(rows, "2026-04-09")
    assert [[r["record_id"] for r in g["rows"]] for g in q] == [["e"], ["h"], ["a", "b"], ["c"]]
    assert q[2]["newest"] == "2026-04-09T03:00:00.000+07:00"
    # lọc ngày do Base (view No File) làm: dòng tạo hôm trước mà view vẫn trả về -> vẫn chạy
    assert build_queue([row("i", ["u7"], "2026-04-08T23:59:00.000+07:00")], "2026-04-09")[0]["rows"][0]["record_id"] == "i"
    # chỉ làm dòng gắn tag GEN (trong view No File); dòng không GEN bị bỏ dù chưa có ảnh
    rows_g = [dict(row("p", ["v1"], "2026-04-09T01:00:00.000+07:00"), gen=True),
              dict(row("q", ["v2"], "2026-04-09T09:00:00.000+07:00"), gen=False)]
    assert [g["rows"][0]["record_id"] for g in build_queue(rows_g, "2026-04-09")] == ["p"]
    assert group_id(q[2]).startswith("S-a-Tee_") and len(group_id(q[1])) <= 80
    # ghi chú
    assert note_ok("2026-10-16", 6, {"tay_sau": 1}) == "OK 2026-10-16 · 6 mảnh + art · gen lại: tay_sau×1"
    assert note_ok("2026-10-16", 4, {}) == "OK 2026-10-16 · 4 mảnh + art"
    assert note_fail("2026-10-16", {"tay_sau": ["sau_giong_truoc"]}) == \
        "QA FAIL 2026-10-16 · tay_sau (sau_giong_truoc) sau 3 lần"
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
    # tối đa 3 lần: lần 1 y nguyên, lần 2-3 có hint (hint lấy 1 lần trước lần 2)
    assert best == {"A": (0, 9), "B": (3, 8), "C": (1, 6)}
    assert fails == {"C": ["mo_nhoe"]} and regen == {"B": 3, "C": 3}
    assert calls["hint"] == 1
    assert calls["regen"] == [(("B", "C"), 1, None), (("B", "C"), 2, "FIX IT"), (("B", "C"), 3, "FIX IT")]
    assert ("A", 0) in calls["keep"] and ("C", 3) in calls["keep"] and ("C", 4) not in calls["keep"]
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
    # #2 release_lock: KHÔNG được xoá lock của tiến trình KHÁC (tránh 1 process gỡ nhầm lock của process kia)
    lk5 = td / "other.lock"; lk5.write_text(str(os.getppid()))   # PID khác (không phải của mình)
    release_lock(lk5)
    assert lk5.exists(), "release_lock không được xoá lock không thuộc PID của mình"
    lk5.unlink()
    # #2 acquire_lock NGUYÊN TỬ: os.open(O_EXCL) phải tự chặn ghi đè dù bước kiểm tra "path.exists()"
    # bị đánh lừa trả False (giả lập race: 2 tiến trình cùng thấy "chưa có lock" ở TOCTOU cũ)
    lk6 = td / "race.lock"; lk6.write_text(str(os.getppid()))   # PID khác, còn sống
    orig_exists = Path.exists
    Path.exists = lambda self: False if self == lk6 else orig_exists(self)
    try:
        try:
            acquire_lock(lk6)
            raise AssertionError("acquire_lock atomic: không được ghi đè lock sống dù exists() bị đánh lừa")
        except SystemExit:
            pass
        assert lk6.read_text() == str(os.getppid()), "lock của tiến trình khác bị ghi đè -> KHÔNG atomic"
    finally:
        Path.exists = orig_exists
    lk6.unlink()
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
    # #2b garment (từ judge.pick, SP chỉ có áo/quần) lọc lại _pieces_json_slugs/_slugs_match dù pieces.json
    # (vision) vẫn trả top_bottom đầy đủ -> tên mảnh mong đợi (và so khớp lần 3) phải bỏ phần SP không có
    assert _pieces_json_slugs(p_same) == same_names                       # không garment -> không lọc
    assert _pieces_json_slugs(p_same, "top") == {"ao_truoc", "ao_sau", "tay_truoc", "tay_sau"}
    assert _pieces_json_slugs(p_same, "bottom") == {"quan_truoc", "quan_sau"}
    assert _slugs_match(p_same, {"ao_truoc", "ao_sau", "tay_truoc", "tay_sau"}, "top") is True
    assert _slugs_match(p_same, same_names, "top") is False                # còn quan_* -> không khớp nữa
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
        # #3 ghi note cuối (write_notes) hết đăng nhập (AuthError) -> PHẢI ném tiếp, không được nuốt
        # (up xong hết nhưng Note không ghi được thì phải dừng đợt, không âm thầm bỏ qua)
        def fake_upload4(rid, fs):
            pass                                            # up thành công hết
        def fake_write_notes_auth(notes):
            raise AuthError("hết đăng nhập lark-cli")
        _lb.get_row, _lb.upload_files, _lb.write_notes = fake_get_row, fake_upload4, fake_write_notes_auth
        notes4 = {"r1": "note4-A", "r2": "note4-B", "r3": "note4-C"}
        done_ids4 = set()
        try:
            upload_rows(rows, files, notes4, False, done_ids4)
            raise AssertionError("AuthError từ write_notes phải ném tiếp")
        except AuthError:
            pass
        assert done_ids4 == {"r1", "r2", "r3"}              # vẫn up xong hết, chỉ ghi note thất bại
        # lỗi khác (không phải AuthError) ở write_notes vẫn chỉ log, KHÔNG được ném (như cũ)
        def fake_write_notes_generic(notes):
            raise RuntimeError("note API lỗi tạm thời")
        _lb.write_notes = fake_write_notes_generic
        notes5 = {"r1": "note5-A", "r2": "note5-B", "r3": "note5-C"}
        done_ids5 = set()
        upload_rows(rows, files, notes5, False, done_ids5)      # không được ném lỗi
        assert done_ids5 == {"r1", "r2", "r3"}
        # #4 dòng ĐÃ có Flat Pieces (vd gen lại đè lần 2) -> bỏ qua up NHƯNG vẫn phải vào done_ids,
        # để 1 dòng khác lỗi quota/StepError sau đó không ghi LỖI đè lên dòng đã xong này
        def fake_get_row2(rid):
            return {"has_pieces": rid == "r1", "pieces": [{"name": "final_a.png", "file_token": f"tok_{rid}"}]}
        def fake_upload5(rid, fs):
            if rid == "r2":
                raise QuotaError("hết hạn mức")
        calls["notes"] = None
        _lb.get_row, _lb.upload_files, _lb.write_notes = fake_get_row2, fake_upload5, fake_write_notes
        notes6 = {"r1": "note6-A", "r2": "note6-B", "r3": "note6-C"}
        done_ids6 = set()
        try:
            upload_rows(rows, files, notes6, False, done_ids6)
            raise AssertionError("QuotaError phải ném tiếp")
        except QuotaError:
            pass
        assert done_ids6 == {"r1"}       # r1 đã có Flat Pieces từ trước -> tính là done
        assert calls["notes"] is None    # r1 bị bỏ qua (không up) -> không có note mới nào để ghi
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
    # #6+7 run_pipe quá giờ -> phải diệt CẢ process group (không chỉ chờ/đợi), không treo đến hết
    # thời gian lệnh thật sự chạy (ở đây là `sleep 30`, giả cho tiến trình pipeline thật)
    if sys.platform != "win32":
        logf6 = td / "pipe_timeout.log"; logf6.write_text("")
        t0 = time.monotonic()
        try:
            run_pipe([], logf6, 0.3, cmd=["sleep", "30"])
            raise AssertionError("run_pipe phải quá giờ")
        except StepError as e:
            assert e.kind == "quá thời gian"
        assert time.monotonic() - t0 < 5, "phải diệt ngay, không chờ hết 30s của lệnh"
        assert not _ACTIVE, "tiến trình quá giờ phải được gỡ khỏi _ACTIVE sau khi xử lý xong"
        # _kill_active: Popen riêng process group (start_new_session) có TIẾN TRÌNH CON (sleep nền)
        # -> killpg phải diệt được CẢ nhóm, không chỉ tiến trình cha (mô phỏng đúng lỗi Codex nêu:
        # subprocess.run chỉ kill con trực tiếp, bỏ sót cháu Codex/OpenArt)
        p = subprocess.Popen(["bash", "-c", "sleep 30 & wait"], start_new_session=True)
        with _ACTIVE_LOCK:
            _ACTIVE.add(p)
        time.sleep(0.2)
        pgid = os.getpgid(p.pid)
        _kill_active()
        p.wait(timeout=5)
        with _ACTIVE_LOCK:
            _ACTIVE.discard(p)
        time.sleep(0.2)
        try:
            os.killpg(pgid, 0)
            raise AssertionError("process group phải bị diệt hết (còn sót tiến trình con sleep)")
        except ProcessLookupError:
            pass
    # #10 rehint: mode "combined" (ảnh ghép nhiều biến thể) phải GIỮ NGUYÊN front+crop của nhóm,
    # bỏ qua front mới của rehint (có thể lạc sang biến thể/SP khác trên cùng ảnh ghép)
    g_combined = {"front": 1, "crop": [0, 0, 500, 1000], "mode": "combined"}
    assert _rehint_target(g_combined, {"front": 2, "crop": [999, 999, 1000, 1000]}) == (1, [0, 0, 500, 1000])
    g_two = {"front": 1, "crop": None, "mode": "two"}
    assert _rehint_target(g_two, {"front": 2, "crop": [10, 10, 20, 20]}) == (2, [10, 10, 20, 20])
    assert _rehint_target(g_two, {"front": None, "crop": None}) == (1, None)
    g_two_b = {**g_two, "back": 2}       # rehint chọn ảnh SAU làm front -> giữ front cũ
    assert _rehint_target(g_two_b, {"front": 2, "crop": None}) == (1, None)
    assert _rehint_target(g_two_b, {"front": 3, "crop": None}) == (3, None)
    g_loc = {"front": 1, "crop": [249, 209, 532, 572], "mode": "infer"}   # đã định vị ô design
    assert _rehint_target(g_loc, {"front": 1, "crop": None}) == (1, [249, 209, 532, 572])
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
    # _run_both: cả 2 callable đều chạy xong; lỗi ở 1 cái -> raise SAU KHI cái kia đã chạy xong
    calls_both = []
    _run_both([lambda: calls_both.append("a"), lambda: calls_both.append("b")])
    assert set(calls_both) == {"a", "b"}

    def ok_fn():
        time.sleep(0.02); calls_both.append("ok")

    def bad_fn():
        calls_both.append("bad"); raise RuntimeError("boom both")
    calls_both.clear()
    try:
        _run_both([bad_fn, ok_fn])
        raise AssertionError("_run_both phải raise khi 1 callable lỗi")
    except RuntimeError as e:
        assert "boom both" in str(e)
    assert calls_both.count("ok") == 1 and calls_both.count("bad") == 1   # cả 2 đều đã chạy xong
    # #8 _run_both: AuthError/QuotaError phải được ưu tiên nổi lên trên lỗi khác, DÙ thứ tự nào
    # (pieces timeout + art AuthError -> phải dừng đợt vì hết đăng nhập, không coi là lỗi nhóm thường)
    def fn_err(e):
        return lambda: (_ for _ in ()).throw(e)
    try:
        _run_both([fn_err(StepError("gen", "timeout")), fn_err(AuthError("hết đăng nhập"))])
        raise AssertionError("phải ném lỗi")
    except AuthError:
        pass
    try:
        _run_both([fn_err(AuthError("a")), fn_err(QuotaError("q"))])   # AuthError ưu tiên hơn QuotaError
        raise AssertionError("phải ném lỗi")
    except AuthError:
        pass
    try:
        _run_both([fn_err(QuotaError("q")), fn_err(StepError("gen", "x"))])
        raise AssertionError("phải ném lỗi")
    except QuotaError:
        pass
    # #1 process_group: judge trả "unassigned" (dòng Claude bỏ sót/không xác định được biến thể) ->
    # phải ghi LỖI RIÊNG cho đúng các dòng đó, KHÔNG được gộp nhầm vào nhóm khác; các nhóm hợp lệ
    # còn lại vẫn xử lý bình thường
    orig_download = globals()["download"]
    orig_pick, orig_sheet = judge.pick, judge.contact_sheet
    orig_subgroup = globals()["process_subgroup"]
    orig_write_notes = lb.write_notes
    calls_pg = {"write": [], "subgroup": []}

    def fake_download2(url, dst, tries=3):
        dst.write_bytes(b"x"); return dst

    def fake_sheet2(rows, out, **kw):
        out = Path(out); out.parent.mkdir(parents=True, exist_ok=True); out.write_bytes(b"x"); return out

    def fake_pick2(sheet, rows, n):
        return {"skip": None, "manual": {}, "unassigned": ["z"],
                "groups": [{"variant": "default", "color": "photo", "matches_photo": True, "front": 1,
                           "back": None, "mode": "infer", "crop": None, "garment": "top_bottom",
                           "record_ids": ["a", "b"]}]}

    def fake_subgroup2(group, rows, pick, srcs, sdir, day, dry, deadline, done_ids, **kw):
        calls_pg["subgroup"].append(sorted(r["record_id"] for r in rows)); return False

    def fake_write_notes2(notes):
        calls_pg["write"].append(dict(notes))

    globals()["download"], judge.pick, judge.contact_sheet = fake_download2, fake_pick2, fake_sheet2
    globals()["process_subgroup"] = fake_subgroup2
    lb.write_notes = fake_write_notes2
    try:
        rows_pg = [{"record_id": "a", "sku": "S-a"}, {"record_id": "b", "sku": "S-b"},
                   {"record_id": "z", "sku": "S-z"}]
        g_pg = {"links": ["u1"], "rows": rows_pg, "newest": "2026-10-16T00:00:00"}
        stats_pg = Counter()
        kind = process_group(g_pg, Path(tempfile.mkdtemp()), "2026-10-16", False, stats_pg)
        assert kind is None
        assert calls_pg["subgroup"] == [["a", "b"]]             # "z" không lọt vào nhóm nào
        assert calls_pg["write"] == [{"z": "LỖI 2026-10-16: judge (không xác định phiên bản) (lần 1)"}]
    finally:
        globals()["download"] = orig_download
        judge.pick, judge.contact_sheet = orig_pick, orig_sheet
        globals()["process_subgroup"] = orig_subgroup
        lb.write_notes = orig_write_notes
    # #2 process_group: 2 nhóm chung 1 ảnh (front=1, vd lưới 8 thiết kế trên 1 tấm) -> phải gọi
    # judge.locate (ảnh lớn + lưới toạ độ, dựng 1 LẦN cho cả 2 nhóm) để định vị chính xác từng biến
    # thể; crop định vị được thì cập nhật vào group (và tới process_subgroup), không định vị được
    # thì giữ crop cũ; kid=True/False truyền đúng theo SKU Design của từng nhóm
    orig_grid_overlay, orig_locate = judge.grid_overlay, judge.locate
    calls_pg2 = {"overlay": [], "locate": [], "subgroup_crop": {}}

    def fake_grid_overlay2(src, out, max_side=1600):
        calls_pg2["overlay"].append(str(src))
        out = Path(out); out.parent.mkdir(parents=True, exist_ok=True); out.write_bytes(b"g"); return out

    def fake_locate2(photo, variant, skus, kid):
        calls_pg2["locate"].append((variant, skus, kid))
        return [100, 100, 600, 600] if variant == "Design 2" else None

    def fake_subgroup3(group, rows, pick, srcs, sdir, day, dry, deadline, done_ids, **kw):
        calls_pg2["subgroup_crop"][group["variant"]] = group["crop"]; return False

    judge.grid_overlay, judge.locate = fake_grid_overlay2, fake_locate2
    globals()["download"], judge.pick, judge.contact_sheet = fake_download2, None, fake_sheet2
    globals()["process_subgroup"] = fake_subgroup3
    lb.write_notes = fake_write_notes2
    calls_pg["write"].clear()

    def fake_pick3(sheet, rows, n):
        return {"skip": None, "manual": {}, "unassigned": [],
                "groups": [{"variant": "Design 2", "color": "photo", "matches_photo": True, "front": 1,
                           "back": None, "mode": "infer", "crop": None, "garment": "top_bottom",
                           "record_ids": ["a"]},
                          {"variant": "Design 3", "color": "photo", "matches_photo": True, "front": 1,
                           "back": None, "mode": "infer", "crop": None, "garment": "top_bottom",
                           "record_ids": ["b"]}]}
    judge.pick = fake_pick3
    try:
        rows_pg2 = [{"record_id": "a", "sku": "S-a-Tee-L-G1"}, {"record_id": "b", "sku": "S-b-Tee-4T-G1"}]
        g_pg2 = {"links": ["u1"], "rows": rows_pg2, "newest": "2026-10-16T00:00:00"}
        stats_pg2 = Counter()
        kind2 = process_group(g_pg2, Path(tempfile.mkdtemp()), "2026-10-16", False, stats_pg2)
        assert kind2 is None
        assert calls_pg2["overlay"] == [calls_pg2["overlay"][0]] * 1       # ảnh front chung -> dựng grid 1 LẦN
        # Design 2 định vị được ngay; Design 3 không được -> Claude thử đủ LOCATE_TRIES lần
        assert sorted(calls_pg2["locate"]) == [("Design 2", "S-a-Tee-L-G1", False)] + \
            [("Design 3", "S-b-Tee-4T-G1", True)] * LOCATE_TRIES
        assert calls_pg2["subgroup_crop"]["Design 2"] == [100, 100, 600, 600]  # định vị được -> cập nhật crop
        # ảnh chung nhiều biến thể mà không định vị được -> KHÔNG gen (gen cả ảnh sẽ tả nhầm design khác)
        assert "Design 3" not in calls_pg2["subgroup_crop"]
    finally:
        globals()["download"] = orig_download
        judge.pick, judge.contact_sheet = orig_pick, orig_sheet
        judge.grid_overlay, judge.locate = orig_grid_overlay, orig_locate
        globals()["process_subgroup"] = orig_subgroup
        lb.write_notes = orig_write_notes
    # nhánh SP không phải quần áo: mỗi dòng 1 art, note OK / QA FAIL riêng, file _QAFAIL khi trượt
    td_nc = Path(tempfile.mkdtemp()); art_nc = td_nc / "a.png"; Image.new("RGB", (8, 8)).save(art_nc)
    orig_run, ups = nonclothing.run, []
    ok_q, bad_q = {"ok": True, "score": 9.0, "errors": []}, {"ok": False, "score": 5.0, "errors": ["sai_chu"]}
    nonclothing.run = lambda rows, *a, **k: [
        (rows[0], {"phang": (art_nc, ok_q), "3d": (art_nc, ok_q)}),
        (rows[1], {"phang": (art_nc, ok_q), "3d": (art_nc, bad_q)})]
    orig_up = upload_rows
    globals()["upload_rows"] = lambda rows, files, notes, dry, done: ups.append(([f.name for f in files], notes))
    try:
        st = Counter()
        rws = [{"record_id": "n1", "sku": "X-Basket-M-G1"}, {"record_id": "n2", "sku": "X-Basket-M-G1"}]
        assert process_nonclothing(rws, [], None, td_nc, "2026-10-16", True, st, set()) is None
    finally:
        nonclothing.run = orig_run; globals()["upload_rows"] = orig_up
    assert ups[0] == (["X-Basket_art_phang.png", "X-Basket_art_3d.png"],
                      {"n1": "OK 2026-10-16 · art phẳng + giả 3D (không phải quần áo)"})
    assert ups[1][0] == ["X-Basket_art_phang.png", "X-Basket_art_3d_QAFAIL.png"]
    assert ups[1][1]["n2"] == "QA FAIL 2026-10-16 · art_3d (sai_chu) sau 3 lần"
    assert st["qafail"] == 1 and st["ok"] == 0
    # LỖI cùng ngày: đếm lượt, quá LOI_MAX thì thôi (cron mỗi giờ không đốt hạn mức mãi)
    assert note_loi("2026-10-16", "tải ảnh") == "LỖI 2026-10-16: tải ảnh (lần 1)"
    assert note_loi("2026-10-16", "gen", "LỖI 2026-10-16: tải ảnh (lần 1)") == "LỖI 2026-10-16: gen (lần 2)"
    assert note_loi("2026-10-16", "gen", "LỖI 2026-10-16: gen") == "LỖI 2026-10-16: gen (lần 2)"
    assert note_loi("2026-10-16", "gen", "LỖI 2026-10-15: gen (lần 5)") == "LỖI 2026-10-16: gen (lần 1)"
    base_r = {"gen": True, "has_pieces": False, "links": ["u"]}
    assert needs_work({**base_r, "note": "LỖI 2026-10-16: gen (lần 1)"}, "2026-10-16")
    assert not needs_work({**base_r, "note": "LỖI 2026-10-16: gen (lần 2)"}, "2026-10-16")
    assert needs_work({**base_r, "note": "LỖI 2026-10-15: gen (lần 2)"}, "2026-10-16")
    # locate thất bại trên ảnh chung nhiều biến thể -> nhóm đó bị loại (không gen cả ảnh)
    orig_go, orig_loc = judge.grid_overlay, judge.locate
    judge.grid_overlay, judge.locate = (lambda src, out, **k: out), (lambda *a, **k: None)
    try:
        gA = {"front": 1, "crop": None, "variant": "Design 1", "color": "x", "record_ids": ["a"]}
        gB = {"front": 1, "crop": None, "variant": "Design 2", "color": "x", "record_ids": ["b"]}
        gC = {"front": 2, "crop": None, "variant": "default", "color": "x", "record_ids": ["c"]}
        rws = [{"record_id": k, "sku": "S-Tee-M", "custom": ""} for k in "abc"]
        assert _locate_groups([gA, gB, gC], rws, [Path("1.jpg"), Path("2.jpg")], Path(tempfile.mkdtemp())) == [gA, gB]
        # chỉ 1 dòng (front không chung) nhưng có Name Custom 'Design 3' -> vẫn loại, không gen cả ảnh
        rws1 = [{"record_id": "a", "sku": "S-Tee-M", "custom": "Design: Design 3"}]
        gA1 = {**gA, "crop": None}
        assert _locate_groups([gA1], rws1, [Path("1.jpg")], Path(tempfile.mkdtemp())) == [gA1]
        # ảnh khớp đầu của Design N (design_photo), không chung nhóm: locate không thấy -> KHÔNG loại (gen cả ảnh)
        gS = {**gA1, "design_photo": True}
        assert _locate_groups([gS], rws1, [Path("1.jpg")], Path(tempfile.mkdtemp())) == []
    finally:
        judge.grid_overlay, judge.locate = orig_go, orig_loc
    # Design N: pick chọn nhầm ảnh -> đổi sang ảnh legend nói đúng; đúng sẵn -> giữ; không legend -> giữ
    judge.find_legend = lambda sheet, n: 8
    judge.match_design = lambda sheet, leg, num, n, garment="outfit": {"desc": "Olaf", "photos": [2, 8]}
    gD = {"front": 4, "crop": [1, 1, 500, 500], "back": None, "back_crop": None, "mode": "combined",
          "variant": "Design 1", "garment": "top_bottom", "record_ids": ["d"]}
    gOK = {**gD, "front": 2, "record_ids": ["e"]}
    rD = [{"record_id": "d", "sku": "X-Design 1-Jumpsuit", "custom": "Design: Design 1"},
          {"record_id": "e", "sku": "X-Design 1-Jumpsuit", "custom": "Design: Design 1"}]
    _check_design_photos([gD, gOK], rD, [Path(f"{i}.jpg") for i in range(1, 12)], Path("s.jpg"))
    assert gD["front"] == 2 and gD["crop"] is None and gOK["front"] == 2 and gOK["crop"] == [1, 1, 500, 500]
    # ảnh riêng của đúng design (photos[0]) -> không locate nhãn 'Design N' trên đó; chung ảnh nhóm khác -> vẫn locate
    assert gD["design_photo"] and _needs_locate(gD, Counter([2]), "Design: Design 1")   # vẫn locate
    # legend có nhưng không ảnh riêng nào khớp -> dùng ảnh legend (không giữ front sai của pick)
    judge.match_design = lambda sheet, leg, num, n, garment="outfit": {"desc": "Elsa", "photos": []}
    gL = {**gD, "front": 3, "mode": "two"}
    _check_design_photos([gL], rD[:1], [Path(f"{i}.jpg") for i in range(1, 12)], Path("s.jpg"))
    assert gL["front"] == 8 and gL["crop"] is None and gL["mode"] == "combined" and not gL["design_photo"]
    judge.find_legend = lambda sheet, n: None
    gN = {**gD, "front": 4}
    _check_design_photos([gN], rD[:1], [Path("1.jpg")] * 11, Path("s.jpg"))
    assert gN["front"] == 4
    # --fix-fails: tên file Base -> slug; thay file: up TRƯỚC rồi mới gỡ đúng mảnh, mảnh khác giữ nguyên
    sku = "VPC082610A09-Wendy 161026-Ugly Sweater-2XL-JO161026"
    assert piece_slug(sku, piece_filename(sku, "ao_sau_phang", failed=True)) == ("ao_sau_phang", True)
    assert piece_slug(sku, piece_filename(sku, "art")) == ("art", False)
    assert piece_slug(sku, "khac.png") == (None, False)
    calls = []
    o_get, o_up, o_rm, o_wn = lb.get_row, lb.upload_files, lb.remove_files, lb.write_notes
    lb.get_row = lambda rid: {"pieces": [{"name": piece_filename(sku, "ao_sau", True), "file_token": "t1"},
                                         {"name": piece_filename(sku, "ao_truoc"), "file_token": "t2"}]}
    lb.upload_files = lambda rid, files: calls.append(("up", len(files)))
    lb.remove_files = lambda rid, toks: calls.append(("rm", toks))
    lb.write_notes = lambda n: calls.append(("note",))
    try:
        replace_rows([{"record_id": "r", "sku": sku}], [Path("x.png")], {"ao_sau"}, {"r": "OK"}, False)
        assert calls == [("up", 1), ("rm", ["t1"]), ("note",)], calls
        calls.clear()
        replace_rows([{"record_id": "r", "sku": sku}], [Path("x.png")], None, {"r": "OK"}, False)
        assert calls == [("up", 1), ("rm", ["t1", "t2"]), ("note",)], calls   # --regen: up trước, gỡ hết sau
    finally:
        lb.get_row, lb.upload_files, lb.remove_files, lb.write_notes = o_get, o_up, o_rm, o_wn
    assert _og_image(b'<meta property="og:image" content="/i/a.jpg?x=1&amp;y=2">', "https://h.io/p/1") == \
        "https://h.io/i/a.jpg?x=1&y=2"
    assert _og_image(b"<html>no image</html>", "https://h.io") is None
    assert _og_image(b"<meta content='//c.io/x.png' property='og:image'>", "https://h.io") == "https://c.io/x.png"
    assert _og_image(b'<link href="/y.jpg" rel="image_src">', "https://h.io/a") == "https://h.io/y.jpg"
    print("batch selfcheck OK")


if __name__ == "__main__":
    main()
