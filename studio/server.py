#!/usr/bin/env python3
"""Flat Studio — web server (aiohttp) lái flat_pipeline.

  python3 server.py [--port 8770]

- Upload ảnh -> chọn mode -> chạy flat_pipeline (subprocess) với --emit vào thư mục run.
- UI poll /state: log + danh sách ảnh (crop, panel, final) hiện dần lên canvas.
- /codex/status + /codex/login: kiểm tra & đăng nhập lại codex khi hết hạn.
- Tự phát hiện code đổi (mtime) -> UI hiện nút Restart.
"""
import argparse, glob, json, os, re, subprocess, sys, time, urllib.request
from pathlib import Path
from aiohttp import web

ROOT = Path(__file__).resolve().parent
HOME = Path(os.path.expanduser("~"))
# Dữ liệu KHÔNG nằm trong studio: mặc định folder cạnh studio (cùng folder cha),
# hoặc override bằng env STUDIO_DATA=<đường dẫn folder tùy chọn>.
DATA = Path(os.getenv("STUDIO_DATA", ROOT.parent / "flat_studio_data")).expanduser()
RUNS = DATA / "runs"
UPLOADS = DATA / "uploads"
RUNS.mkdir(parents=True, exist_ok=True); UPLOADS.mkdir(parents=True, exist_ok=True)
PIPELINE = ROOT / "flat_pipeline.py"
CODEX_AUTH = HOME / ".codex" / "auth.json"
CODEX_LOGIN_LOG = DATA / "codex_login.log"
SRC = [ROOT / "server.py", ROOT / "index.html", PIPELINE]

MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
# slug mảnh (khớp flat_pipeline) -> parse tên file final_[<person>_]<slug>.png để gắn nút gen lại
KNOWN_SLUGS = ["ao_truoc", "ao_sau", "quan_truoc", "quan_sau",
               "tay_truoc", "tay_sau", "tay_trai_truoc", "tay_trai_sau",
               "tay_phai_truoc", "tay_phai_sau", "than_truoc", "than_sau", "ta_truoc", "ta_sau"]
# codex hết hạn: log thật thường là 'token refresh failed: HTTP 401' / 'access token expired'
# (không kèm chữ 'codex') -> bắt trực tiếp các dấu hiệu này, khỏi cần chữ 'codex' đứng trước.
CODEX_ERR = re.compile(
    r"token refresh failed|access token expired|http 401|\bunauthor|requires newer codex"
    r"|codex.*(login|auth|expired|401|token)", re.I)

def _parse_piece(stem):
    """final_<slug> hoặc final_<person>_<slug> -> (person, slug); None nếu không phải mảnh."""
    if not stem.startswith("final_"):
        return None
    rest = stem[len("final_"):]
    for slug in KNOWN_SLUGS:
        if rest == slug:
            return (1, slug)
        m = re.fullmatch(rf"(\d+)_{re.escape(slug)}", rest)
        if m:
            return (int(m.group(1)), slug)
    return None

_procs = {}   # run_id -> Popen


def _src_mtime():
    return max((f.stat().st_mtime for f in SRC if f.exists()), default=0)


_NOCACHE = {"Cache-Control": "no-cache"}   # HTML luôn revalidate -> không serve bản cũ đã cache

async def index(request):
    return web.FileResponse(ROOT / "index.html", headers=_NOCACHE)


async def media(request):
    p = Path(os.path.realpath(request.query.get("p", "")))
    if not str(p).startswith(str(RUNS)) and not str(p).startswith(str(UPLOADS)):
        return web.Response(status=403, text="forbidden")
    if not p.is_file():
        return web.Response(status=404, text="not found")
    return web.FileResponse(p, headers={"Content-Type": MIME.get(p.suffix.lower(), "application/octet-stream")})


IMG_CT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}

async def reveal(request):
    """Mở thư mục chứa ảnh bằng file manager của HĐH (chỉ trong data/)."""
    p = Path(os.path.realpath(request.query.get("p", "")))
    if not (str(p).startswith(str(RUNS)) or str(p).startswith(str(UPLOADS))) or not p.exists():
        return web.json_response({"error": "not found"}, status=404)
    d = str(p.parent)
    try:
        if sys.platform == "win32":
            os.startfile(d)                                    # noqa: Windows Explorer
        elif sys.platform == "darwin":
            subprocess.Popen(["open", d])
        else:
            subprocess.Popen(["xdg-open", d])
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)
    return web.json_response({"ok": True})


async def upload(request):
    reader = await request.multipart()
    saved = []
    async for part in reader:
        ct = (part.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        ext = os.path.splitext(part.filename or "")[1].lower()
        if ext not in IMG_CT.values():                 # tên file thiếu/không rõ -> lấy từ content-type
            ext = IMG_CT.get(ct, ".png" if ct.startswith("image/") else "")
        if not ext:                                    # không phải ảnh -> bỏ
            continue
        dst = UPLOADS / f"up_{int(time.time()*1000)}_{len(saved)}{ext}"
        with open(dst, "wb") as f:
            while chunk := await part.read_chunk():
                f.write(chunk)
        saved.append(str(dst))
    if not saved:
        return web.json_response({"error": "không đọc được ảnh (clipboard rỗng?)"}, status=400)
    return web.json_response({"paths": saved})


async def fetch_url(request):
    """Tải ảnh từ URL (khi kéo-thả/dán ảnh từ web) -> lưu vào uploads."""
    body = await request.json()
    url = (body.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return web.json_response({"error": "URL không hợp lệ"}, status=400)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            ct = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
            data = resp.read(40 * 1024 * 1024)
    except Exception as e:
        return web.json_response({"error": f"tải ảnh lỗi: {e}"}, status=400)
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
           "image/gif": ".gif"}.get(ct, "")
    if not ext:
        for e2 in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            if url.lower().split("?")[0].endswith(e2):
                ext = e2; break
    if not ext:
        return web.json_response({"error": "không phải ảnh"}, status=400)
    dst = UPLOADS / f"web_{int(time.time()*1000)}{ext}"
    dst.write_bytes(data)
    return web.json_response({"path": str(dst)})


async def run(request):
    body = await request.json()
    mode = body.get("mode", "pieces")   # pieces | art
    front = body.get("front"); back = body.get("back")
    combined = bool(body.get("combined"))
    flat = bool(body.get("flat"))        # True -> vẽ phẳng vector; False -> giữ 3D
    only = body.get("only") or []        # danh sách slug -> chỉ gen mảnh đó
    if not front or not Path(front).is_file():
        return web.json_response({"error": "chưa có ảnh front"}, status=400)
    rid = time.strftime("%Y%m%d-%H%M%S")
    rundir = RUNS / rid; rundir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(PIPELINE), "--front", front,
           "-o", str(rundir / "final.png"), "--emit", str(rundir),
           "--log", str(rundir / "run.log")]
    if mode == "art":
        cmd += ["--art"]
    else:                                # pieces (mặc định)
        cmd += ["--pieces"]
        if combined:
            cmd += ["--combined"]
        elif back and Path(back).is_file():
            cmd += ["--back", back]
        if only:
            cmd += ["--only", ",".join(only)]
        if flat:
            cmd += ["--flat"]
    p = subprocess.Popen(cmd, cwd=str(ROOT))
    _procs[rid] = p
    return web.json_response({"run": rid})


async def regen(request):
    """Gen lại 1 mảnh của 1 run cũ: dùng lại pieces.json (KHÔNG vision lại) -> chỉ gen slug đó."""
    body = await request.json()
    rid = body.get("run", ""); slug = body.get("slug", "")
    try:
        person = int(body.get("person", 1) or 1)
    except (TypeError, ValueError):
        person = 1
    rundir = RUNS / rid
    if not rundir.is_dir() or slug not in KNOWN_SLUGS:
        return web.json_response({"error": "run/slug không hợp lệ"}, status=400)
    pdir = rundir / f"person_{person}"
    if not (pdir / "pieces.json").is_file():   # run 2 ảnh / combined: pieces.json nằm thẳng trong run
        pdir = rundir
    cache = pdir / "pieces.json"; front = pdir / "crop_front.png"
    if not cache.is_file() or not front.is_file():
        return web.json_response({"error": "thiếu pieces.json/crop để gen lẻ"}, status=400)
    multi = len(list(rundir.glob("person_*.png"))) > 1
    out = rundir / (f"final_{person}.png" if multi else "final.png")
    cmd = [sys.executable, str(PIPELINE), "--pieces", "--front", str(front),
           "--cache", str(cache), "--only", slug, "-o", str(out),
           "--emit", str(pdir), "--log", str(rundir / "run.log")]
    if bool(body.get("flat")):
        cmd += ["--flat"]
    back = pdir / "crop_back.png"
    if back.is_file():
        cmd += ["--back", str(back)]
    _procs[rid] = subprocess.Popen(cmd, cwd=str(ROOT))
    return web.json_response({"ok": True, "run": rid})


def _item(p, name):
    from urllib.parse import quote
    return {"src": "/media?p=" + quote(str(p)), "path": str(p), "name": name}

def _list_images(rundir: Path):
    """Trả về ảnh theo nhóm để UI xếp lên canvas (kèm path thật để mở thư mục)."""
    groups = {"crop": [], "panel": [], "final": []}
    # gộp cả run trực tiếp lẫn các thư mục con person_* (nhiều người)
    for base in [rundir] + [p for p in sorted(rundir.glob("person_*")) if p.is_dir()]:
        tag = base.name if base != rundir else ""
        for c in sorted(base.glob("crop_*.png")):
            groups["crop"].append(_item(c, (tag + " " + c.stem).strip()))
    for f in sorted(rundir.glob("final*.png")):
        it = _item(f, f.stem)
        pp = _parse_piece(f.stem)
        if pp:
            it["person"], it["slug"] = pp     # -> UI gắn nút gen lại
        groups["final"].append(it)
    return groups


async def state(request):
    rid = request.query.get("run", "")
    rundir = RUNS / rid
    if not rundir.is_dir():
        return web.json_response({"error": "run không tồn tại"}, status=404)
    logf = rundir / "run.log"
    logtxt = logf.read_text(encoding="utf-8", errors="replace") if logf.exists() else ""
    p = _procs.get(rid)
    running = p is not None and p.poll() is None
    codex_bad = bool(CODEX_ERR.search(logtxt))
    return web.json_response({
        "log": logtxt, "images": _list_images(rundir),
        "running": running, "codex_bad": codex_bad,
        "src_changed": _src_mtime() > request.app["boot_mtime"],
    })


async def codex_status(request):
    ok = CODEX_AUTH.is_file()
    return web.json_response({"ok": ok, "reason": "" if ok else "chưa đăng nhập codex",
                             "src_changed": _src_mtime() > request.app["boot_mtime"]})


async def codex_login(request):
    """Chạy `codex login`, ghi output ra file để UI đọc URL/mã device."""
    CODEX_LOGIN_LOG.write_text("Đang khởi động codex login...\n", encoding="utf-8")
    with open(CODEX_LOGIN_LOG, "a", encoding="utf-8") as lf:
        try:
            subprocess.Popen(["codex", "login"], stdout=lf, stderr=lf,
                             cwd=str(ROOT), shell=(sys.platform == "win32"))
        except Exception as e:
            lf.write(f"Lỗi chạy codex login: {e}\n")
    return web.json_response({"ok": True})


async def codex_login_log(request):
    txt = CODEX_LOGIN_LOG.read_text(encoding="utf-8", errors="replace") if CODEX_LOGIN_LOG.exists() else ""
    return web.json_response({"log": txt, "ok": CODEX_AUTH.is_file()})


async def restart(request):
    """Thoát process để start.bat/khởi động lại nạp code mới."""
    async def _bye():
        import asyncio
        await asyncio.sleep(0.3); os._exit(0)
    import asyncio; asyncio.ensure_future(_bye())
    return web.json_response({"ok": True})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    a = ap.parse_args()
    app = web.Application(client_max_size=256 * 1024 * 1024)
    app["boot_mtime"] = _src_mtime()
    app.add_routes([
        web.get("/", index), web.get("/media", media), web.get("/reveal", reveal),
        web.post("/upload", upload), web.post("/fetch_url", fetch_url),
        web.post("/run", run), web.post("/regen", regen), web.get("/state", state),
        web.get("/codex/status", codex_status), web.post("/codex/login", codex_login),
        web.get("/codex/login/log", codex_login_log), web.post("/restart", restart),
    ])
    print(f"Flat Studio chạy ở http://127.0.0.1:{a.port}")
    web.run_app(app, host="127.0.0.1", port=a.port, print=None)


if __name__ == "__main__":
    main()
