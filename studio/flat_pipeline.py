#!/usr/bin/env python3
"""
Ảnh trang phục mặc thật -> các MẢNH rập PHẲNG rời (cut-and-sew, in tràn). MỘT script.

2 mode duy nhất (không ghép thành phẩm cuối nữa — xuất từng miếng):
  --pieces : auto nhận VÁY vs ÁO+QUẦN -> mỗi mảnh 1 file.
             * áo/quần: full-bleed chữ nhật (tràn kín khung, nền vải kể cả trắng)
             * váy    : cắt theo silhouette cong thật (nền ngoài trong suốt)
             mặt sau: 1 ảnh -> tự suy từ họa tiết trước; --back (2 ảnh) / --combined
             (1 ảnh có cả 2 mặt) -> dùng lưng THẬT. Tách từng người (song song đa key).
  --art    : 1 bản vẽ CAD phẳng nguyên bộ, mặt trước. Tách từng người.

Pipeline: dola-seed (BytePlus ARK) NHÌN ảnh -> phân loại + tả print từng mảnh ->
gen (primary chatgpt-imagegen/codex, lỗi -> OpenArt Seedream 4.5).

Chạy:
  python3 flat_pipeline.py --pieces --front f.png [--back b.png | --combined] -o out.png
  python3 flat_pipeline.py --art    --front f.png -o out.png
  python3 flat_pipeline.py --selfcheck

Yêu cầu: codex đã đăng nhập + `openart login`; ARK_API_KEY (env hoặc .env cạnh script).
"""
import argparse, base64, io, json, os, re, subprocess, sys, tempfile, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_T0 = time.time()
_LOGF = None  # file handle nếu ghi log ra file
_LOG_LOCK = threading.Lock()   # gen song song nhiều mảnh -> log() không bị xen dòng

def _open_log(path):
    global _LOGF
    p = Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    _LOGF = open(p, "a", encoding="utf-8")
    return p

def log(stage, msg=""):
    """Log 1 dòng có mốc thời gian + tên giai đoạn -> stdout VÀ file (flush ngay)."""
    line = f"[+{time.time() - _T0:6.1f}s][{stage:<9}] {msg}"
    with _LOG_LOCK:
        print(line, flush=True)
        if _LOGF:
            _LOGF.write(line + "\n"); _LOGF.flush()

EMIT = None   # nếu set: crop/panel ghi thẳng vào thư mục này (cho UI web poll)
COLOR = None  # --color: ép màu nền vải (batch: nhóm con màu khác ảnh gốc)
HINT = None   # --hint: ghi chú sửa lỗi QA ghép cuối prompt (batch: gen lại lần 3-4)
GARMENT = None  # --garment top|bottom|top_bottom|dress: ép loại SP thật (batch, từ judge.pick); None = để vision tự quyết

def _prompt_extra():
    """Phần ghép thêm vào MỌI prompt gen theo --color/--hint (rỗng nếu không truyền)."""
    s = ""
    if COLOR:
        s += (f" RECOLOUR: the garment's BASE fabric colour must be {COLOR}; keep every graphic, "
              "motif and its position exactly — only the base fabric colour changes.")
    if HINT:
        s += " FIX: " + HINT
    return s

def _workdir(prefix, sub=""):
    """Thư mục làm việc: dùng EMIT (được serve) nếu có, không thì temp."""
    if EMIT:
        d = Path(EMIT) / sub if sub else Path(EMIT)
        d.mkdir(parents=True, exist_ok=True)
        return d
    return Path(tempfile.mkdtemp(prefix=prefix))

import cv2, numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

HERE = Path(__file__).resolve().parent

def _mac_ca_bundle():
    """macOS: Python python.org không đọc Keychain -> urllib (chatgpt-imagegen) lỗi
    CERTIFICATE_VERIFY_FAILED. Gộp CA của Keychain hệ thống (gồm cả cert proxy/antivirus
    công ty) + certifi thành 1 file, set SSL_CERT_FILE cho mọi tiến trình con."""
    if sys.platform != "darwin":
        return
    pem = ""
    for kc in ("/System/Library/Keychains/SystemRootCertificates.keychain",
               "/Library/Keychains/System.keychain"):
        try:
            pem += subprocess.run(["security", "find-certificate", "-a", "-p", kc],
                                  capture_output=True, text=True, timeout=30).stdout
        except Exception:
            pass
    try:
        import certifi
        pem += Path(certifi.where()).read_text()
    except Exception:
        pass
    if "BEGIN CERTIFICATE" in pem:
        f = Path(tempfile.gettempdir()) / "dsds_ca_bundle.pem"
        f.write_text(pem)
        os.environ["SSL_CERT_FILE"] = os.environ["REQUESTS_CA_BUNDLE"] = str(f)
_mac_ca_bundle()

ASSETS = HERE / "assets"
VSC = Path(os.getenv("VSC_ROOT", "/mnt/6C96C1A096C16AE2/vsc"))
# ưu tiên .env cạnh script (self-contained), không có thì dùng .env gốc của chatgpt-api
_DEF_ENV = (HERE / ".env") if (HERE / ".env").exists() else (VSC / "chatgpt-api" / ".env")
ENV_FILE = Path(os.getenv("ARK_ENV_FILE", _DEF_ENV))
ARK_BASE = "https://ark.ap-southeast.bytepluses.com/api/v3"
VMODELS = ["seed-2-0-lite-260428",   # model chính (dola-seed lite)
           "seed-2-0-mini-260428"]   # backup (dola-seed mini)
OA_MODEL = "byte-plus-seedream-4-5"

# --- gen prompt: MẢNH VÁY (cắt cong) -> vẽ trên nền xám rồi cutout() theo silhouette ---
# TRUNG THỰC: giữ nguyên chi tiết/bóng/hiệu ứng nổi giả-3D của họa tiết in, chỉ tách mảnh
# (nền xám chỉ để cutout cắt silhouette, KHÔNG làm phẳng họa tiết).
DRESS_FAITHFUL = (
    "A single cut-and-sew garment PANEL laid flat, front view, on a plain flat light grey background and "
    "NOTHING else (so the panel can be cut out cleanly). REPRODUCE the panel's printed artwork FAITHFULLY "
    "exactly as in the reference photo — keep every colour, detail, shading, soft highlight and any raised "
    "faux-3D / moulded look; do NOT flatten or simplify it into plain vector. No human body, no head, no "
    "arms, no legs, no hanger, no mockup.")
# PHẲNG: vẽ vector phẳng như bản gốc (bỏ hết bóng/nổi/3D) — dùng khi user chọn "Art phẳng" cho mảnh.
DRESS_FLAT = (
    "A 2D FLAT VECTOR sewing-pattern PANEL, absolutely flat and symmetric, front view, on a plain flat "
    "light grey background and NOTHING else (so the panel can be cut out). REDRAW the print as CLEAN flat "
    "vector shapes — this is NOT a photo. SOLID FLAT COLOR FILL only, crisp clean edges like a technical "
    "CAD flat: NO fabric texture, NO shading, NO soft highlight, NO gradient, NO drop shadow, NO 3D, NO "
    "moulded look, NO photo-realism, completely matte. Clean geometric shapes + clean solid/dashed stitch "
    "lines. No human body, no head, no arms, no legs, no hanger, no mockup.")
SHAPE_BODICE = " Shape: a sleeveless bodice tank panel like the second reference (shape guide)."
SHAPE_FAN = (" Shape: a wide quarter-circle CIRCLE-SKIRT fan panel spread flat like the second "
             "reference (shape guide). Every band follows the curved arc, parallel to the curved hem.")
# tà váy: bỏ dây/nơ/sash thắt lủng lẳng (USER: mảnh tà không có dây thừa) — xoá khỏi desc + nhắc CUỐI prompt
NO_TIES = (" IMPORTANT: draw ONLY the flat skirt fabric panel. NO tie, sash, knot, bow, ribbon, string, "
           "cord or strap anywhere — not hanging, not draped, not tied at the hip — even though the photo "
           "shows one. The fan outline must be clean and unbroken.")
_TIE_RE = re.compile(r"\b(sash|tie|ties|tied|knot|bow|ribbon|string|cord|drape)", re.I)

def _strip_ties(desc):
    """Bỏ các cụm (ngăn bởi ';') nhắc tới dây/nơ/sash khỏi mô tả tà váy."""
    return "; ".join(p.strip() for p in desc.split(";") if not _TIE_RE.search(p))

# --- gen prompt: MẢNH ÁO/QUẦN/TAY (full-bleed) -> mỗi mảnh in tràn kín khung, không viền ---
# TRUNG THỰC: tái hiện y họa tiết in kể cả bóng/nổi/giả-3D, chỉ tách 1 panel & tràn mép.
# Neo {base} = màu nền vải (parse từ desc): panel THƯA chi tiết thì model hay tô đen/void
# vùng trơn -> ép "vùng không họa tiết giữ màu nền, cấm void/glow".
PANEL_BLEED = (
    "A full-bleed textile PRINT for the {label} — the printed artwork of ONE panel only, isolated from "
    "the whole garment. REPRODUCE this panel's printed artwork EXACTLY as in the reference photo: keep "
    "every detail, colour, shading, soft highlight, bevel and the raised faux-3D / moulded-armour look; "
    "do NOT flatten, simplify or turn it into plain vector. The panel's BASE fabric colour is {base} and "
    "fills any area with no artwork (if white, use white); NEVER replace empty areas with black, void, "
    "glow, vignette or transparency. Zoom in so the artwork FILLS 100% of the frame and BLEEDS OFF all "
    "four edges: NO margin, NO border, NO letterboxing, NO garment silhouette/outline, NO hood, NO "
    "collar, NO cuffs, NO person, NO hanger, NO mockup — just this one panel's print, corner to corner. "
    "The print of this panel: {desc}")

# PHẲNG: vẽ VECTOR PHẲNG SẠCH như bản gốc (CAD flat) — REDRAW thành vector, KHÔNG trace ảnh.
PANEL_BLEED_FLAT = (
    "A 2D FLAT VECTOR technical CAD flat of the {label}. REDRAW the panel's print as CLEAN FLAT VECTOR "
    "ART — SOLID flat colour blocks with crisp clean edges, absolutely flat, matte and simplified, like a "
    "sewing-pattern diagram. This is NOT a photo: NO 3D, NO shading, NO soft highlight, NO bevel, NO "
    "gradient, NO drop shadow, NO fabric texture, NO photo-realism, no noise. Redraw every graphic as "
    "clean geometric shapes and clean solid/dashed stitch lines. The BASE fabric colour is {base} and "
    "fills any area with no graphic (if white, use pure white); NEVER a black void, glow, vignette or "
    "transparency. Fill the whole frame and bleed off all four edges: NO margin, NO border, NO garment "
    "silhouette/outline, NO hood, NO collar, NO cuffs, NO person, NO hanger, NO mockup. "
    "The print of this panel: {desc}")

def _base_colour(desc):
    """Màu nền vải để neo prompt — vision luôn mở đầu desc bằng 'base <màu>; ...'."""
    m = re.match(r"\s*base\s+([^;.,]+)", desc or "", re.I)
    return m.group(1).strip() if m else "the panel's own solid fabric colour"

# --- gen prompt: ART PHẲNG (1 bản vẽ nguyên bộ, mặt trước) ---
OUTFIT_FLAT = (
    "A 2D FLAT technical fashion-flat drawing of a COMPLETE outfit laid flat, FRONT view, on a plain "
    "light grey background. Draw the TOP garment and the BOTTOM garment as flat, symmetric garment "
    "silhouettes, the top placed above the bottom, both fully visible. SOLID FLAT COLOR FILL only, "
    "crisp clean edges like a technical CAD flat: NO fabric texture, NO 3D, NO shading, NO gradient, "
    "NO shadow, NO folds/wrinkles. NO person, no head, no hands, no feet, no hanger, no mockup. "
    "Redraw EVERY visible front detail faithfully in the right place and colour. "
    "TOP garment: {top}. BOTTOM garment: {bottom}.")
# SP chỉ có áo hoặc chỉ có quần (USER RULE: không vẽ phần SP không có) -> 1 mảnh art riêng.
OUTFIT_FLAT_TOP = (
    "A 2D FLAT technical fashion-flat drawing of a TOP garment laid flat, FRONT view, on a plain light "
    "grey background. Draw it as a flat, symmetric garment silhouette, fully visible. SOLID FLAT COLOR "
    "FILL only, crisp clean edges like a technical CAD flat: NO fabric texture, NO 3D, NO shading, NO "
    "gradient, NO shadow, NO folds/wrinkles. NO person, no head, no hands, no feet, no hanger, no mockup. "
    "Redraw EVERY visible front detail faithfully in the right place and colour. "
    "TOP garment: {top}.")
OUTFIT_FLAT_BOTTOM = (
    "A 2D FLAT technical fashion-flat drawing of a BOTTOM garment (pants/shorts/skirt) laid flat, FRONT "
    "view, on a plain light grey background. Draw it as a flat, symmetric garment silhouette, fully "
    "visible. SOLID FLAT COLOR FILL only, crisp clean edges like a technical CAD flat: NO fabric texture, "
    "NO 3D, NO shading, NO gradient, NO shadow, NO folds/wrinkles. NO person, no head, no hands, no feet, "
    "no hanger, no mockup. Redraw EVERY visible front detail faithfully in the right place and colour. "
    "BOTTOM garment: {bottom}.")


# ---------- 1. dola-seed vision ----------
def _ark_keys():
    """Nhiều key -> vision chạy song song thật (ARK serialize theo key). Nạp từ
    ARK_API_KEYS='k1,k2,...' và/hoặc ARK_API_KEY, ARK_API_KEY2, ARK_API_KEY3..."""
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_FILE)
    except Exception:
        pass
    keys = []
    for k in (os.getenv("ARK_API_KEYS") or "").split(","):
        k = k.strip()
        if k and k not in keys:
            keys.append(k)
    for name in ("ARK_API_KEY", "ARK_API_KEY2", "ARK_API_KEY3", "ARK_API_KEY4"):
        v = os.getenv(name)
        if v and v not in keys:
            keys.append(v)
    return keys

def _ark_clients():
    keys = _ark_keys()
    if not keys:
        sys.exit("Thiếu ARK_API_KEY (env hoặc chatgpt-api/.env).")
    from openai import OpenAI
    return [OpenAI(api_key=k, base_url=ARK_BASE) for k in keys]

def _ark_client():
    return _ark_clients()[0]

# Vision chỉ cần bbox/mô tả -> gửi ảnh THU NHỎ cho nhanh (không đổi kết quả chuẩn hoá).
VISION_MAXSIDE = int(os.getenv("FLAT_VISION_MAXSIDE", "1152"))

def _vision_uri(img, maxside=VISION_MAXSIDE):
    im = Image.open(img)
    if max(im.size) > maxside:
        r = maxside / max(im.size)
        im = im.resize((max(1, int(im.width * r)), max(1, int(im.height * r))), Image.LANCZOS)
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

_CIG = None
def _codex_vision(content, timeout=240):
    """Vision dự phòng: GPT qua endpoint Codex của chatgpt-imagegen (dùng lại auth `codex login`,
    không tool vẽ) -> trả text. `content` = định dạng chat của ARK."""
    global _CIG
    if _CIG is None:
        import importlib.machinery, importlib.util
        ld = importlib.machinery.SourceFileLoader("chatgpt_imagegen", CHATGPT_BIN)
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(ld.name, ld))
        ld.exec_module(mod)
        _CIG = mod                          # gán SAU khi nạp xong: luồng song song không thấy module dở
    access, account, _ = _CIG._extract_access_token(_CIG._load_auth())
    if not access:
        raise RuntimeError("codex chưa đăng nhập")
    # ponytail: không tự refresh token hết hạn; bước gen (chatgpt-imagegen) tự refresh rồi lần sau chạy được
    parts = [{"type": "input_image", "image_url": c["image_url"]["url"]} if c["type"] == "image_url"
             else {"type": "input_text", "text": c["text"]} for c in content]
    body = {"model": os.getenv("CHATGPT_IMAGEGEN_MODEL", "gpt-5.6-sol"), "stream": True,
            "instructions": "You are an image generation assistant.",   # endpoint chỉ nhận instructions allowlist
            "input": [{"type": "message", "role": "user", "content": parts}],
            "store": False, "reasoning": {"effort": "low"}, "text": {"verbosity": "low"}}
    headers = _CIG._build_headers(access, account, _CIG._detect_codex_version())
    for ev in _CIG._stream(_CIG.CODEX_BACKEND, headers, body, time.monotonic() + timeout, 120):
        if ev.get("type") == "response.output_text.done":
            return ev.get("text", "")
        if ev.get("type") in ("response.failed", "response.incomplete", "error"):
            raise RuntimeError(f"codex: {str(ev)[:120]}")
    return ""

def _vision(client, img, prompt, need=()):
    """Gọi vision với fallback qua VMODELS, log từng lần thử. `need` = key bắt buộc trong JSON.
    `img` là 1 đường dẫn HOẶC list nhiều đường dẫn (gộp vào 1 request — ARK serialize
    theo key nên 1 request nhiều ảnh nhanh hơn nhiều request rời)."""
    imgs = img if isinstance(img, (list, tuple)) else [img]
    content = [{"type": "text", "text": prompt}]
    for i in imgs:
        content.append({"type": "image_url", "image_url": {"url": _vision_uri(i)}})
    last = None
    for m in ["codex"] + VMODELS:          # codex (GPT) nhanh ~3x; dola fallback
        try:
            log("vision", f"thử model {m} ...")
            if m == "codex":
                txt, fin = _codex_vision(content), "stop"
            else:
                r = client.chat.completions.create(model=m,
                    messages=[{"role": "user", "content": content}])
                txt, fin = r.choices[0].message.content or "", r.choices[0].finish_reason
            if fin == "content_filter":
                raise ValueError("BytePlus chặn ảnh (content_filter: ảnh/hình in nhạy cảm)")
            hit = re.search(r"\{.*\}", txt, re.S)
            if not hit:
                raise ValueError(f"model không trả JSON (finish={fin}): {txt[:60]!r}")
            js = json.loads(hit.group())
            for k in need:
                if k not in js:
                    raise ValueError(f"thiếu key '{k}'")
            log("vision", f"OK ({m})")
            return js, m
        except Exception as e:
            last = f"{m}: {str(e)[:90]}"
            log("vision", f"lỗi {m}: {str(e)[:70]}")
    sys.exit(f"Vision lỗi hết model. Cuối: {last}")

# --- vision: tách người (mode nhiều người) ---
PEOPLE_PROMPT = (
    "This photo shows several people standing in a row, each wearing an outfit. Return ONLY JSON "
    '{"people":[[x0,y0,x1,y1],...]} ordered LEFT TO RIGHT, one TIGHT bounding box per person '
    "covering that person's full outfit (shoulders to hem), integers normalized 0-1000 (x from left, "
    "y from top). Exclude neighboring people from each box as much as possible.")

def seed_people(client, img):
    w, h = Image.open(img).size
    js, m = _vision(client, img, PEOPLE_PROMPT, need=("people",))
    # ảnh chỉ có đồ (không người) -> codex trả [] -> coi cả ảnh là 1 trang phục
    return js["people"] or [[0, 0, 1000, 1000]], (w, h), m

# --- vision: PIECES = phân loại đồ + tả print từng mảnh (mọi phần đều trước/sau) ---
_PIECES_SCHEMA = (
    "Classify the outfit, then describe each cut-and-sew PANEL as a concise flat-technical PRINT spec. "
    "Every desc MUST start with 'base <colour>;' then every graphic/motif and its position (colours as "
    "words or hex; no prose). If a detail looks raised/embossed/moulded/3D-shaded, note that. Return ONLY JSON. "
    "If it is a DRESS (one-piece: sleeveless bodice + skirt): "
    '{"type":"dress","bodice_front":"..","bodice_back":"..","skirt_front":"..","skirt_back":".."}. '
    "If it is a TOP + BOTTOM (a separate upper garment + pants/shorts): "
    '{"type":"top_bottom","top_front":"..","top_back":"..","pants_front":"..","pants_back":"..",'
    '"sleeves_same":true,"sl_front":"..","sl_back":"..","sr_front":"..","sr_back":".."}. '
    "top_*/pants_* = the TORSO/BODY resp. LEGS panel (top EXCLUDES sleeves, hood, collar). "
    "sl_front/sl_back = the LEFT sleeve's front-facing / back-facing print; sr_front/sr_back = the RIGHT "
    "sleeve's. sleeves_same = true if both sleeves carry the SAME print (then sr_* may repeat sl_*), "
    "false if the two sleeves clearly differ. "
    "If it is a TOP ONLY (no separate bottom garment — e.g. a T-shirt/sweatshirt/hoodie, even if its print "
    "depicts fake pants/shorts/underwear/legs below the waist): "
    '{"type":"top","top_front":"..","top_back":"..","sleeves_same":true,"sl_front":"..","sl_back":"..",'
    '"sr_front":"..","sr_back":".."}. '
    "If it is a BOTTOM ONLY (pants/jogger/shorts, or a skirt — describe the skirt in pants_* too): "
    '{"type":"bottom","pants_front":"..","pants_back":".."}. '
    "Printed underwear/legs/body on the LOWER part of a top are PART OF THE TOP's print, not a separate "
    "bottom garment. "
    "Realistic body parts printed ON the garment (muscles, abs, chest, skin, tattoos) are part of the "
    "PRINT: describe them as artwork on the panel, never as bare skin or an opening. "
    "For skirt_* do NOT describe loose ties/sashes/knots/strings hanging off the skirt (they are left out). "
    'ALSO add "boxes": {"<panel key>": [x0,y0,x1,y1], ...} with one TIGHT bounding box per panel key '
    "above, integers normalized 0-1000 (x from left, y from top) on the image where that panel is seen "
    "(front panels on the front image/view, back panels on the back image/view). Each box covers ONLY "
    "that panel (a bodice box stops at the waist seam, a skirt box starts there; a torso box excludes "
    "the sleeves). If a back panel is not visible (inferred), give the box of the matching FRONT panel.")
_PIECES_INTRO = {
    "infer": ("This photo shows the FRONT of a person's outfit. " + _PIECES_SCHEMA +
              " Only the front is visible: INFER each *_back / *_sau panel from the front — keep the same "
              "base colour, yokes and trims, but a plainer body; drop front-only graphics/badges/zips "
              "unless they clearly wrap around to the back."),
    "two": ("You are given TWO photos of the SAME outfit: the FIRST image is the FRONT, the SECOND is "
            "the BACK. " + _PIECES_SCHEMA +
            " Describe front panels from the first image and back panels from the second (real back)."),
    "combined": ("This ONE photo shows the SAME outfit from TWO angles side by side: a FRONT view and a "
                 "BACK view. Decide which is which. " + _PIECES_SCHEMA +
                 " Describe front panels from the front view and back panels from the back view (real back)."),
}

def _pieces_prompt(source):
    """Prompt vision cho seed_pieces; ép GARMENT (--garment) vào cuối prompt nếu có truyền."""
    p = _PIECES_INTRO[source]
    if GARMENT:
        p += f" The product is known to be: {GARMENT} — use exactly that type."
    return p

def seed_pieces(client, imgs, source):
    """imgs: 1 path (infer/combined) hoặc [front, back] (two). Trả JSON {type, panel descs}."""
    try:
        js, _ = _vision(client, imgs, _pieces_prompt(source), need=("type", "boxes"))
    except SystemExit:      # không model nào trả box -> như cũ: mô tả, gen từ ảnh nguyên
        log("vision", "không model nào trả box -> gen từ ảnh nguyên")
        js, _ = _vision(client, imgs, _pieces_prompt(source), need=("type",))
    return js

# nhãn gửi model cho từng slug full-bleed (PANEL_BLEED {label})
_BLEED_LABEL = {
    "ao_truoc": "front torso/body panel of the top (no sleeves, no hood)",
    "ao_sau":   "back torso/body panel of the top (no sleeves, no hood)",
    "quan_truoc": "front of the pants", "quan_sau": "back of the pants",
    "tay_truoc": "front-facing panel of the sleeve", "tay_sau": "back-facing panel of the sleeve",
    "tay_trai_truoc": "front-facing panel of the LEFT sleeve", "tay_trai_sau": "back-facing panel of the LEFT sleeve",
    "tay_phai_truoc": "front-facing panel of the RIGHT sleeve", "tay_phai_sau": "back-facing panel of the RIGHT sleeve",
}

def _filter_by_garment(items, garment):
    """Lọc theo garment thật (--garment, từ judge.pick): top -> bỏ quan_*; bottom -> bỏ ao_*/tay_*.
    `items` = plan (list dict có 'slug') HOẶC tập/list slug string (batch.py dùng lại để lọc tên mảnh
    mong đợi từ pieces.json) -> trả về cùng kiểu (list/set) đầu vào."""
    def slug(x):
        return x["slug"] if isinstance(x, dict) else x
    if garment == "top":
        keep = lambda x: not slug(x).startswith("quan_")
    elif garment == "bottom":
        keep = lambda x: not (slug(x).startswith("ao_") or slug(x).startswith("tay"))
    else:
        return items
    return type(items)(x for x in items if keep(x))

_BACK_FALLBACK = {"top_back": "top_front", "pants_back": "pants_front",
                  "bodice_back": "bodice_front", "skirt_back": "skirt_front"}

def _desc(js, key):
    """Mô tả panel theo `key`; vision bỏ sót *_back -> suy từ mặt trước tương ứng (USER DECISION:
    SP có phần nào thì luôn đủ 2 mặt của phần đó, không được thiếu mảnh sản xuất)."""
    return js.get(key) or js.get(_BACK_FALLBACK.get(key, ""))

def _box(js, key):
    """Box 0-1000 của panel `key` (vision 'boxes'); hỏng/nhỏ (<15% mỗi chiều) -> None (dùng ảnh nguyên).
    Tay áo (sl_/sr_) luôn None: thử thật, box tay áo của vision hay lệch (trúng cổ/đầu người bên cạnh)."""
    if key.startswith(("sl_", "sr_")):
        return None
    boxes = js.get("boxes") if isinstance(js.get("boxes"), dict) else {}
    b = boxes.get(key)
    if not (isinstance(b, list) and len(b) == 4 and all(isinstance(v, (int, float)) for v in b)):
        return None
    x0, x1 = sorted((max(0, b[0]), min(1000, b[2])))
    y0, y1 = sorted((max(0, b[1]), min(1000, b[3])))
    return [int(x0), int(y0), int(x1), int(y1)] if x1 - x0 >= 150 and y1 - y0 >= 150 else None

def _pieces_plan(js):
    """JSON vision -> danh sách mảnh cần gen: mỗi mảnh dict(slug, kind, desc, side[, shape, guide, label]).
    kind='dress' (cắt cong) | 'bleed' (full-bleed). side='front'/'back' -> chọn ảnh ref.
    type 'top'/'bottom' -> chỉ mảnh áo/tay hoặc chỉ mảnh quần (SP chỉ có 1 phần). GARMENT (--garment,
    biết trước loại SP thật từ SKU) lọc lại plan nếu vision vẫn trả loại khác (vd top_bottom thừa quần)."""
    typ = (js.get("type") or "top_bottom").strip()
    if typ not in ("dress", "top_bottom", "top", "bottom"):
        typ = "top_bottom"
    plan = []
    if typ == "dress":
        for slug, shape, guide, key, side in [
            ("than_truoc", SHAPE_BODICE, "shape_bodice.png", "bodice_front", "front"),
            ("than_sau",   SHAPE_BODICE, "shape_bodice.png", "bodice_back",  "back"),
            ("ta_truoc",   SHAPE_FAN,    "shape_fan.png",    "skirt_front",  "front"),
            ("ta_sau",     SHAPE_FAN,    "shape_fan.png",    "skirt_back",   "back"),
        ]:
            desc = _desc(js, key)
            if desc:
                plan.append(dict(slug=slug, kind="dress", desc=desc, side=side, shape=shape, guide=guide,
                                 box=_box(js, key)))
        return plan
    if typ != "bottom":   # top / top_bottom: thân áo
        for slug, key, side in [("ao_truoc", "top_front", "front"), ("ao_sau", "top_back", "back")]:
            desc = _desc(js, key)
            if desc:
                plan.append(dict(slug=slug, kind="bleed", desc=desc, side=side, label=_BLEED_LABEL[slug],
                                 box=_box(js, key)))
    if typ != "top":      # bottom / top_bottom: quần
        for slug, key, side in [("quan_truoc", "pants_front", "front"), ("quan_sau", "pants_back", "back")]:
            desc = _desc(js, key)
            if desc:
                plan.append(dict(slug=slug, kind="bleed", desc=desc, side=side, label=_BLEED_LABEL[slug],
                                 box=_box(js, key)))
    if typ != "bottom":   # top / top_bottom: tay áo — giống -> 1 bộ trước/sau; khác -> trái + phải
        if js.get("sleeves_same", True):
            sleeves = [("tay_truoc", "sl_front", "front"), ("tay_sau", "sl_back", "back")]
        else:
            sleeves = [("tay_trai_truoc", "sl_front", "front"), ("tay_trai_sau", "sl_back", "back"),
                       ("tay_phai_truoc", "sr_front", "front"), ("tay_phai_sau", "sr_back", "back")]
        for slug, key, side in sleeves:
            desc = js.get(key) or js.get(key.replace("sr_", "sl_"))   # thiếu tay phải -> mượn trái
            if desc:
                plan.append(dict(slug=slug, kind="bleed", desc=desc, side=side, label=_BLEED_LABEL[slug],
                                 box=_box(js, key)))
    if GARMENT and GARMENT != typ:
        plan = _filter_by_garment(plan, GARMENT)
    return plan

def _flatten_white(path):
    """Mảnh full-bleed phải ĐỤC (nền vải thật). Codex đôi khi trả PNG có nền trắng = trong suốt
    -> ghép phẳng lên trắng để không bị 'thủng' khi đặt lên mockup tối."""
    # ponytail: ghép lên TRẮNG (màu vải phổ biến nhất); đồ nền tối hầu như không có vùng trong suốt.
    im = Image.open(path)
    if im.mode in ("RGBA", "LA") or "transparency" in im.info:
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        bg.save(path)

def _slug_wanted(slug, want):
    """Khớp slug với danh sách chọn (--only). Tay: chọn 'tay_truoc'/'tay_sau' (chung) khớp cả
    biến thể trái/phải (tay_trai_truoc, tay_phai_sau, ...) — vì lúc chọn chưa biết 2 tay giống hay khác."""
    if slug in want:
        return True
    if slug.startswith("tay"):
        side = "truoc" if slug.endswith("truoc") else "sau"
        return f"tay_{side}" in want
    return False

CROP_NOTE = (" The FIRST reference image is a close crop of exactly this panel: draw ONLY what is in "
             "that crop. The LAST reference shows the whole garment for context only — do NOT copy other "
             "parts of it (other panels, skirt, pants, sleeves) into this panel.")

def _make_piece(spec, refs, work, out, flat=False):
    """Gen 1 mảnh -> file <out.stem>_<slug><suffix>. Váy: cắt cong (cutout); áo/quần/tay: full-bleed.
    flat=True -> vẽ VECTOR phẳng (bỏ 3D/bóng); flat=False (mặc định) -> giữ chi tiết/nổi giả-3D."""
    dst = out.with_name(f"{out.stem}_{spec['slug']}{out.suffix}")
    ref = refs[spec["side"]]
    # có box -> Python cắt riêng vùng mảnh làm ref CHÍNH (ảnh 1), ảnh nguyên để CUỐI chỉ làm ngữ cảnh
    # -> model không vẽ lấn sang phần đồ khác (vd lưng thân dính váy). Không box -> như cũ.
    crop, ctx = None, []
    if spec.get("box"):
        crop = work / f"crop_{spec['slug']}.png"
        crop_norm(ref, spec["box"], Image.open(ref).size, crop)
        ctx = [ref]
    note = CROP_NOTE if crop else ""
    if spec["kind"] == "dress":
        tmp = work / f"gen_{spec['slug']}.png"
        base = DRESS_FLAT if flat else DRESS_FAITHFUL
        fan = spec["shape"] is SHAPE_FAN
        desc = _strip_ties(spec["desc"]) if fan else spec["desc"]
        gen_panel(base + spec["shape"] + note + " " + desc + (NO_TIES if fan else ""),
                  [crop or ref, ASSETS / spec["guide"], *ctx], tmp)
        cutout(tmp).save(dst)                               # cắt theo silhouette, nền trong suốt
    else:
        tpl = PANEL_BLEED_FLAT if flat else PANEL_BLEED
        gen_panel(tpl.format(label=spec["label"], base=COLOR or _base_colour(spec["desc"]), desc=spec["desc"])
                  + note, [crop or ref, *ctx], dst)
        _flatten_white(dst)                                 # full-bleed: đục, nền vải trắng
    return dst

# --- vision: ART = tả cả bộ (áo trên + quần dưới) ---
OUTFIT_PROMPT = (
    "This is a photo of a person wearing a full outfit. Describe the ENTIRE outfit as a precise "
    'flat-technical spec so it can be redrawn as a fashion-flat. Return ONLY JSON: '
    '{"top":{"desc":"..."},"bottom":{"desc":"..."}}. '
    "top.desc = the upper garment (or the top half of a dress): type, base fabric colour, and EVERY "
    "visible FRONT detail with colours and positions — chest graphics/panels, buttons, badges, pockets, "
    "zipper, hood, collar, sleeve prints, cuffs. "
    "bottom.desc = the lower garment (or skirt): type, base colour, and every visible front detail — "
    "knee patches, side stripes, pockets, waistband, hems. "
    "If there is no separate bottom (or top) garment, set its desc to 'none'. "
    "Colours as words or hex. Be specific and positional; no prose outside the spec. "
    "Realistic body parts printed ON the garment (muscles, abs, chest, skin, tattoos) are part of the "
    "PRINT: describe them as artwork on the panel, never as bare skin or an opening.")

def seed_outfit(client, img):
    w, h = Image.open(img).size
    js, m = _vision(client, img, OUTFIT_PROMPT, need=("top", "bottom"))
    return js, (w, h), m

def crop_norm(img, box, wh, out):
    w, h = wh
    x0, y0, x1, y1 = (box[0]*w//1000, box[1]*h//1000, box[2]*w//1000, box[3]*h//1000)
    x0, x1 = sorted((max(0, x0), min(w, x1)))
    y0, y1 = sorted((max(0, y0), min(h, y1)))
    Image.open(img).crop((x0, y0, x1, y1)).save(out)
    return (x0, y0, x1, y1)


# ---------- 2. gen: chatgpt-imagegen (primary) -> OpenArt Seedream 4.5 (fallback) ----------
def _bin(name):
    cands = [name + ".exe", name] if sys.platform == "win32" else [name]
    for c in cands:
        b = HERE / "bin" / c
        if b.exists():
            return str(b)
    return name
CHATGPT_BIN = _bin("chatgpt-imagegen")
OPENART_BIN = _bin("openart")

def _launcher(path):
    """File là script python (shebang) -> chạy qua interpreter (bắt buộc trên Windows)."""
    try:
        with open(path, "rb") as f:
            first = f.readline(200)
        if first.startswith(b"#!") and b"python" in first:
            return [sys.executable]
    except Exception:
        pass
    return []

def _win_native_ok(path):
    """True nếu file chạy được trên nền tảng hiện tại. Trên Windows, binary ELF (Linux)
    không chạy được -> False để báo lỗi rõ thay vì WinError 193 khó hiểu."""
    if sys.platform != "win32":
        return True
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
        return magic != b"\x7fELF"          # ELF = binary Linux, không phải PE Windows
    except Exception:
        return True

def _run(cmd, timeout):
    # Ép child in UTF-8 + đọc UTF-8 -> tránh UnicodeError khi đường dẫn có dấu tiếng Việt.
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       env=env, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip() or f"rc={p.returncode}")

def _chatgpt_gen(prompt, refs, out, timeout=420):
    cmd = _launcher(CHATGPT_BIN) + [CHATGPT_BIN, prompt, "-o", str(out), "--size", "auto",
           "--backend", "codex", "--quiet"]
    for r in refs:
        cmd += ["-i", str(r)]
    _run(cmd, timeout)
    if not Path(out).exists():
        raise RuntimeError("chatgpt-imagegen không tạo file")
    return out

def _openart_gen(prompt, refs, out, timeout=300):
    if not _win_native_ok(OPENART_BIN):
        raise RuntimeError("OpenArt chỉ có bản Linux, không chạy được trên Windows "
                           "(chỉ dùng chatgpt-imagegen làm gen chính)")
    cmd = _launcher(OPENART_BIN) + [OPENART_BIN, "generate", "image", prompt, "--model", OA_MODEL,
           "-o", str(out), "--yes", "--compact"]
    for r in refs:
        cmd += ["--image", str(r)]
    _run(cmd, timeout)
    if not Path(out).exists():
        raise RuntimeError("openart không tạo file")
    return out

def gen_panel(prompt, refs, out):
    """Primary chatgpt-imagegen (codex), lỗi -> fallback OpenArt Seedream 4.5."""
    prompt = prompt + _prompt_extra()
    try:
        log("gen", "  → thử chatgpt-imagegen (codex) ...")
        r = _chatgpt_gen(prompt, refs, out)
        log("gen", "  ✓ xong bằng chatgpt-imagegen")
        return r
    except Exception as e:
        log("gen", "  ✗ chatgpt-imagegen lỗi -> fallback OpenArt. Lỗi đầy đủ:")
        log("gen", "  " + str(e)[:1500].replace("\n", "\n  "))
    r = _openart_gen(prompt, refs, out)
    log("gen", "  ✓ xong bằng OpenArt Seedream 4.5")
    return r


# ---------- 3. cutout theo ngưỡng nền (cho mảnh váy: cắt cong, nền trong suốt) ----------
def cutout(path, T=12):
    # PIL đọc được path Unicode (dấu tiếng Việt) trên Windows; cv2.imread thì KHÔNG (trả None).
    rgb = np.array(Image.open(path).convert("RGB"))       # RGB sẵn, không cần đảo BGR
    im = rgb.astype(np.int16); h, w = im.shape[:2]
    corners = np.concatenate([im[:20, :20].reshape(-1, 3), im[:20, -20:].reshape(-1, 3),
                              im[-20:, :20].reshape(-1, 3), im[-20:, -20:].reshape(-1, 3)])
    bg = np.median(corners, 0)
    fg = (np.abs(im - bg).max(2) > T).astype(np.uint8)
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(fg, 8)
    if n > 1:
        fg = (lab == 1 + np.argmax(stats[1:, cv2.CC_STAT_AREA])).astype(np.uint8)
    fg = ndimage.binary_fill_holes(fg).astype(np.uint8)
    al = cv2.GaussianBlur((fg * 255).astype(np.uint8), (3, 3), 0)
    rgba = np.dstack([rgb, al]); ys, xs = np.where(fg > 0)
    return Image.fromarray(rgba[ys.min():ys.max() + 1, xs.min():xs.max() + 1])


# ---------- 4. workers từng người ----------
GEN_WORKERS = int(os.getenv("FLAT_GEN_WORKERS", "3"))   # gen song song mấy mảnh cùng lúc (codex tự xếp hàng tối đa 4)

MIN_SIDE = 1024

def _upscaled(src, dst, min_side=MIN_SIDE):
    """Lưu src -> dst, phóng (LANCZOS) nếu cạnh dài < min_side: ảnh nhỏ (vd 1 ô cắt từ ảnh 8 design,
    ~290px) làm vision trả box lệch và crop mảnh quá mờ."""
    im = Image.open(src)
    if max(im.size) < min_side:
        k = min_side / max(im.size)
        im = im.resize((round(im.width * k), round(im.height * k)), Image.LANCZOS)
    im.save(dst)
    return dst

def run_pieces_one(client, img, out, emit_sub="", back=None, combined=False, only=None, cache=None, flat=False):
    """1 người/1 trang phục -> mỗi mảnh 1 file.
      áo+quần: full-bleed chữ nhật. váy: cắt cong (cutout), nền trong suốt.
      mặt sau: back (2 ảnh) / combined (1 ảnh 2 mặt) -> lưng thật; else tự suy từ trước.
      only = chỉ gen các slug này; cache = nạp lại mô tả vision cũ, BỎ vision (gen lẻ)."""
    work = _workdir("pieces_", emit_sub)
    ref_front = _upscaled(img, work / "crop_front.png")
    if back:
        ref_back = _upscaled(back, work / "crop_back.png")
    else:
        ref_back = ref_front   # combined: ảnh chứa cả 2 mặt; infer: dùng ảnh trước làm style-ref
    refs = {"front": ref_front, "back": ref_back}
    if cache:                                   # gen lẻ: dùng lại mô tả cũ, KHÔNG vision lại
        js = json.loads(Path(cache).read_text(encoding="utf-8"))
        log("crop", f"dùng lại mô tả cũ ({Path(cache).name})")
    else:
        source = "two" if back else ("combined" if combined else "infer")
        vimgs = [ref_front, ref_back] if back else ref_front   # ảnh đã phóng: vision định vị box chuẩn hơn
        log("crop", "nhìn ảnh: phân loại đồ + tả print từng mảnh ...")
        js = seed_pieces(client, vimgs, source)
        (work / "pieces.json").write_text(json.dumps(js, ensure_ascii=False), encoding="utf-8")
    typ = (js.get("type") or "top_bottom").strip()
    tay = "" if typ == "dress" else f" | tay {'khác' if not js.get('sleeves_same', True) else 'giống'}"
    log("crop", f"loại: {typ}{tay}")
    plan = _pieces_plan(js)
    if not back and not combined:   # mặt sau tự suy: box sau nằm trên ảnh TRƯỚC -> cắt ra thì chép y mặt trước
        plan = [{**sp, "box": None} if sp["side"] == "back" else sp for sp in plan]
    if only:
        want = set(only if isinstance(only, (list, tuple, set)) else str(only).split(","))
        plan = [s for s in plan if _slug_wanted(s["slug"], want)]
    outs, errs = [], []
    with ThreadPoolExecutor(max_workers=GEN_WORKERS) as ex:
        futs = []
        for spec in plan:
            log("gen", f"gen mảnh {spec['slug']} ({'phẳng' if flat else '3D'}) ...")
            futs.append((spec, ex.submit(_make_piece, spec, refs, work, out, flat=flat)))
        for spec, fut in futs:                  # đợi theo đúng thứ tự plan -> outs giữ nguyên thứ tự
            try:
                outs.append(fut.result())
            except Exception as e:              # 1 mảnh lỗi -> làm tiếp mảnh khác (batch gen lại sau)
                log("gen", f"  ✗ mảnh {spec['slug']} lỗi: {str(e)[:300]}")
                errs.append(e)
    log("done", f"{len(outs)} mảnh: " + ", ".join(o.name for o in outs))
    if errs and not outs:
        raise errs[-1]
    return outs[0] if outs else out

def _art_prompt(top, bot):
    """Chọn template art theo GARMENT (--garment, biết trước SP chỉ có áo/quần) nếu có, không thì theo
    desc 'none'/rỗng (vision tự nhận SP không có phần đó)."""
    none_top = not top or top.strip().lower() == "none"
    none_bot = not bot or bot.strip().lower() == "none"
    if GARMENT == "top" or (GARMENT is None and none_bot and not none_top):
        return OUTFIT_FLAT_TOP.format(top=top)
    if GARMENT == "bottom" or (GARMENT is None and none_top and not none_bot):
        return OUTFIT_FLAT_BOTTOM.format(bottom=bot)
    return OUTFIT_FLAT.format(top=top, bottom=bot)

def run_art_one(client, img, out, emit_sub="", **_):
    """1 người: vision tả cả bộ -> gen 1 bản vẽ phẳng, mặt trước (chỉ áo / chỉ quần / cả bộ tuỳ SP)."""
    work = _workdir("art_", emit_sub)
    log("crop", "nhìn ảnh: tả chi tiết cả bộ ...")
    js, _, _ = seed_outfit(client, img)
    top = js["top"].get("desc", "") if isinstance(js["top"], dict) else str(js["top"])
    bot = js["bottom"].get("desc", "") if isinstance(js["bottom"], dict) else str(js["bottom"])
    log("crop", f"trên: {top[:60]}… | dưới: {bot[:60]}…")
    ref = work / "crop_outfit.png"
    Image.open(img).save(ref)
    log("gen", "gen bản vẽ phẳng ...")
    gen_panel(_art_prompt(top, bot), [ref], out)
    log("done", str(out))
    return out


# ---------- 5. orchestration: tách từng người (song song đa key) ----------
def _run_perperson(img, out, worker, label, wprefix, **wkw):
    """Tách từng người -> mỗi người chạy `worker` -> out_1..N (1 người: out).
    Nhiều người + nhiều key -> chạy song song."""
    log("start", f"{label} | {Path(img).name}")
    clients = _ark_clients()
    log("detect", "nhìn ảnh, tách từng người ...")
    boxes, wh, _ = seed_people(clients[0], img)
    log("detect", f"phát hiện {len(boxes)} người")
    work = _workdir(wprefix)
    single = len(boxes) == 1

    def one(i, box):
        person = work / f"person_{i}.png"
        crop_norm(img, box, wh, person)
        dst = out if single else out.with_name(f"{out.stem}_{i}{out.suffix}")
        log("person", f"=== {i}/{len(boxes)} -> {dst.name} ===")
        try:
            return worker(clients[(i - 1) % len(clients)], str(person), dst,
                          emit_sub=f"person_{i}", **wkw)
        except Exception as e:
            log("person", f"  người {i} lỗi, bỏ qua: {str(e)[:140]}")
            return None

    items = list(enumerate(boxes, 1))
    if len(items) > 1 and len(clients) > 1:   # nhiều người + nhiều key -> song song
        with ThreadPoolExecutor(max_workers=min(len(clients), len(items))) as ex:
            outs = [d for d in ex.map(lambda t: one(*t), items) if d]
    else:
        outs = [d for d in (one(i, b) for i, b in items) if d]
    log("done", f"{label} {len(outs)}/{len(boxes)}: " + ", ".join(o.name for o in outs))
    return outs

def run_pieces(front, out, back=None, combined=False, only=None, cache=None, flat=False, single=False):
    """Tách mảnh. 1 ảnh (tự suy sau) -> tách từng người. --back/--combined/--single -> 1 trang phục.
    only = chỉ gen slug đã chọn; cache = gen lẻ dùng lại mô tả cũ; flat = vẽ phẳng vector thay vì 3D."""
    if back or combined or cache or single:
        client = _ark_client()
        tag = "gen lẻ (cache)" if cache else ("+sau thật (2 ảnh)" if back else
                                             ("2-view (1 ảnh)" if combined else "1 bộ (--single)"))
        log("start", f"PIECES tách mảnh | {Path(front).name} | {tag} | {'phẳng' if flat else '3D'}")
        run_pieces_one(client, front, Path(out), back=back, combined=combined, only=only, cache=cache, flat=flat)
        return [Path(out)]
    return _run_perperson(front, Path(out), run_pieces_one, "PIECES tách mảnh", "piecesset_", only=only, flat=flat)

def run_art(front, out, single=False):
    """Art phẳng: 1 bản vẽ nguyên bộ mặt trước. Tách từng người (--single: không tách)."""
    if single:
        log("start", f"ART phẳng nguyên bộ | {Path(front).name} | 1 bộ (--single)")
        return [run_art_one(_ark_client(), front, Path(out))]
    return _run_perperson(front, Path(out), run_art_one, "ART phẳng nguyên bộ", "artset_")


def _selfcheck():
    # crop_norm scale/clamp: 0-1000 -> pixel, clamp trong khung, tự sửa thứ tự
    im = Image.new("RGB", (1000, 500), "white")
    p = Path(tempfile.mktemp(suffix=".png")); im.save(p)
    assert crop_norm(p, [100, 200, 900, 400], (1000, 500), Path(tempfile.mktemp(suffix=".png"))) == (100, 100, 900, 200)
    assert crop_norm(p, [900, 0, 100, 999], (1000, 500), Path(tempfile.mktemp(suffix=".png"))) == (100, 0, 900, 499)  # sort x, scale y
    # 3 nguồn suy mặt sau đều có prompt riêng, đều ép JSON có "type" + đủ mảnh 2 mặt
    for src in ("infer", "two", "combined"):
        pr = _PIECES_INTRO[src]
        assert '"type"' in pr and "top_back" in pr and "bodice_back" in pr, src
        assert "never as bare skin or an opening" in pr, src     # hình cơ thể in = họa tiết, không phải da thật
    assert "never as bare skin or an opening" in OUTFIT_PROMPT
    # plan: váy -> 4 mảnh; tay giống -> 6 (tay_truoc/sau); tay khác -> 8 (trái/phải × trước/sau)
    d = _pieces_plan({"type": "dress", "bodice_front": "base a", "bodice_back": "b",
                      "skirt_front": "c", "skirt_back": "d"})
    assert [s["slug"] for s in d] == ["than_truoc", "than_sau", "ta_truoc", "ta_sau"]
    assert all(s["kind"] == "dress" for s in d)
    tb = dict(type="top_bottom", top_front="base white", top_back="b", pants_front="c", pants_back="d",
              sl_front="e", sl_back="f", sr_front="g", sr_back="h")
    same = _pieces_plan({**tb, "sleeves_same": True})
    assert {s["slug"] for s in same} == {"ao_truoc", "ao_sau", "quan_truoc", "quan_sau", "tay_truoc", "tay_sau"}
    diff = _pieces_plan({**tb, "sleeves_same": False})
    assert {s["slug"] for s in diff} == {"ao_truoc", "ao_sau", "quan_truoc", "quan_sau",
                                         "tay_trai_truoc", "tay_trai_sau", "tay_phai_truoc", "tay_phai_sau"}
    # type "top"/"bottom": SP chỉ có 1 phần -> chỉ gen phần đó (USER RULE: không gen phần SP không có)
    top_only = _pieces_plan({"type": "top", "top_front": "base white; x", "top_back": "b",
                             "sleeves_same": True, "sl_front": "e", "sl_back": "f"})
    assert {s["slug"] for s in top_only} == {"ao_truoc", "ao_sau", "tay_truoc", "tay_sau"}
    bottom_only = _pieces_plan({"type": "bottom", "pants_front": "c", "pants_back": "d"})
    assert {s["slug"] for s in bottom_only} == {"quan_truoc", "quan_sau"}
    # #9 vision bỏ sót mô tả lưng (*_back) -> suy từ mặt trước tương ứng, KHÔNG được thiếu mảnh sản
    # xuất (USER DECISION: pants_back/top_back/bodice_back/skirt_back thiếu -> lấy đúng desc front)
    bottom_missing_back = _pieces_plan({"type": "bottom", "pants_front": "base red; x"})
    assert {s["slug"] for s in bottom_missing_back} == {"quan_truoc", "quan_sau"}
    assert next(s["desc"] for s in bottom_missing_back if s["slug"] == "quan_sau") == "base red; x"
    top_missing_back = _pieces_plan({"type": "top", "top_front": "base blue; y", "sleeves_same": True,
                                     "sl_front": "e"})
    assert {s["slug"] for s in top_missing_back} == {"ao_truoc", "ao_sau", "tay_truoc"}
    assert next(s["desc"] for s in top_missing_back if s["slug"] == "ao_sau") == "base blue; y"
    dress_missing_back = _pieces_plan({"type": "dress", "bodice_front": "base x", "skirt_front": "base y"})
    assert {s["slug"] for s in dress_missing_back} == {"than_truoc", "than_sau", "ta_truoc", "ta_sau"}
    assert next(s["desc"] for s in dress_missing_back if s["slug"] == "than_sau") == "base x"
    assert next(s["desc"] for s in dress_missing_back if s["slug"] == "ta_sau") == "base y"
    assert '"type":"top"' in _PIECES_SCHEMA and '"type":"bottom"' in _PIECES_SCHEMA
    assert "not a separate bottom garment" in _PIECES_SCHEMA
    # GARMENT (--garment, biết trước loại SP thật) ép lọc plan dù vision vẫn trả top_bottom đầy đủ
    globals()["GARMENT"] = "top"
    assert {s["slug"] for s in _pieces_plan({**tb, "sleeves_same": True})} == \
        {"ao_truoc", "ao_sau", "tay_truoc", "tay_sau"}
    globals()["GARMENT"] = "bottom"
    assert {s["slug"] for s in _pieces_plan({**tb, "sleeves_same": True})} == {"quan_truoc", "quan_sau"}
    assert "known to be: bottom" in _pieces_prompt("infer")
    globals()["GARMENT"] = None
    assert "known to be" not in _pieces_prompt("infer")
    # 2 kiểu vẽ: PANEL_BLEED (3D) vs PANEL_BLEED_FLAT (phẳng) format OK; váy có DRESS_FLAT
    for tpl in (PANEL_BLEED, PANEL_BLEED_FLAT):
        assert "{" not in tpl.format(label="x", base="white", desc="y")
    assert "FLAT VECTOR" in DRESS_FLAT and "NO 3D" in PANEL_BLEED_FLAT
    # art templates mới (SP chỉ có áo / chỉ có quần) format OK, không còn {} sót lại
    assert "{" not in OUTFIT_FLAT_TOP.format(top="x")
    assert "{" not in OUTFIT_FLAT_BOTTOM.format(bottom="y")
    assert "none" in OUTFIT_PROMPT
    # _art_prompt: GARMENT ép chọn template; không có GARMENT thì theo desc 'none'/rỗng
    assert _art_prompt("a top", "none").startswith("A 2D FLAT technical fashion-flat drawing of a TOP")
    assert _art_prompt("", "a bottom").startswith("A 2D FLAT technical fashion-flat drawing of a BOTTOM")
    assert _art_prompt("a top", "a bottom").startswith("A 2D FLAT technical fashion-flat drawing of a COMPLETE")
    globals()["GARMENT"] = "top"
    assert _art_prompt("a top", "a bottom").startswith("A 2D FLAT technical fashion-flat drawing of a TOP")
    globals()["GARMENT"] = None
    # cutout: đọc file (PIL, an toàn path Unicode) -> cắt vật thể theo silhouette, ra RGBA nhỏ hơn
    im2 = Image.new("RGB", (60, 60), "white"); ImageDraw.Draw(im2).rectangle([20, 20, 40, 40], fill="red")
    cp = Path(tempfile.mktemp(suffix=".png")); im2.save(cp)
    co = cutout(cp)
    assert co.mode == "RGBA" and co.width < 60 and co.height < 60
    # flatten: PNG trong suốt -> đục nền trắng (mảnh full-bleed không bị thủng)
    q = Path(tempfile.mktemp(suffix=".png")); Image.new("RGBA", (4, 4), (10, 20, 30, 0)).save(q)
    _flatten_white(q)
    assert Image.open(q).convert("RGB").getpixel((0, 0)) == (255, 255, 255)
    # --only: chọn tay chung khớp cả biến thể trái/phải; không lẫn sang áo/quần
    assert _slug_wanted("tay_trai_truoc", {"tay_truoc"}) and _slug_wanted("tay_phai_sau", {"tay_sau"})
    assert not _slug_wanted("tay_trai_sau", {"tay_truoc"})
    assert _slug_wanted("ao_sau", {"ao_sau"}) and not _slug_wanted("ao_sau", {"tay_sau"})
    # neo màu nền: parse 'base <màu>' để ép panel thưa không tô đen/void
    assert _base_colour("base white; yoke yellow") == "white"
    assert _base_colour("base mustard yellow #E6A817; x") == "mustard yellow #E6A817"
    assert _base_colour("no base word") == "the panel's own solid fabric colour"
    # --color / --hint: ghép vào cuối prompt; mặc định rỗng
    globals()["COLOR"], globals()["HINT"] = None, None
    assert _prompt_extra() == ""
    globals()["COLOR"], globals()["HINT"] = "heliconia pink", "NO chest graphic on back"
    ex = _prompt_extra()
    assert "heliconia pink" in ex and "FIX: NO chest graphic on back" in ex
    globals()["COLOR"], globals()["HINT"] = None, None
    # GEN_WORKERS: đọc FLAT_GEN_WORKERS (mặc định 3)
    assert GEN_WORKERS == int(os.getenv("FLAT_GEN_WORKERS", "3"))
    # run_pieces_one chạy song song (ThreadPoolExecutor): giữ thứ tự theo plan, 1 mảnh lỗi
    # không chặn mảnh khác, tất cả lỗi -> raise lỗi cuối cùng (theo thứ tự plan)
    cache_dir = Path(tempfile.mkdtemp())
    cache_path = cache_dir / "pieces.json"
    cache_path.write_text(json.dumps({"type": "top_bottom", "top_front": "base white; x", "top_back": "b",
                                      "pants_front": "c", "pants_back": "d", "sleeves_same": True,
                                      "sl_front": "e", "sl_back": "f"}), encoding="utf-8")
    img_in = cache_dir / "front.png"; Image.new("RGB", (10, 10), "white").save(img_in)
    log_calls = []
    orig_log, orig_make_piece = globals()["log"], globals()["_make_piece"]

    def fake_log(stage, msg=""):
        log_calls.append((stage, msg))

    def fake_make_piece(spec, refs, work, out, flat=False):
        if spec["slug"] == "quan_sau":
            raise RuntimeError("boom quan_sau")
        time.sleep(0.01)
        return out.with_name(f"{out.stem}_{spec['slug']}{out.suffix}")

    globals()["log"], globals()["_make_piece"] = fake_log, fake_make_piece
    try:
        result = run_pieces_one(None, img_in, cache_dir / "final.png", cache=cache_path)
    finally:
        globals()["log"], globals()["_make_piece"] = orig_log, orig_make_piece
    assert result == cache_dir / "final_ao_truoc.png"          # mảnh đầu plan, không lỗi
    assert any(s == "gen" and m.startswith("  ✗ mảnh quan_sau lỗi: boom quan_sau") for s, m in log_calls)
    done_msg = next(m for s, m in log_calls if s == "done")
    assert done_msg == "5 mảnh: " + ", ".join(
        f"final_{slug}.png" for slug in ["ao_truoc", "ao_sau", "quan_truoc", "tay_truoc", "tay_sau"])
    # tất cả mảnh lỗi -> raise lỗi CUỐI CÙNG theo thứ tự plan (tay_sau)
    def fail_all(spec, refs, work, out, flat=False):
        raise RuntimeError(f"fail {spec['slug']}")
    globals()["log"], globals()["_make_piece"] = fake_log, fail_all
    try:
        try:
            run_pieces_one(None, img_in, cache_dir / "final2.png", cache=cache_path)
            raise AssertionError("phải raise khi tất cả mảnh lỗi")
        except RuntimeError as e:
            assert "fail tay_sau" in str(e)
    finally:
        globals()["log"], globals()["_make_piece"] = orig_log, orig_make_piece
    # box từng mảnh: hợp lệ -> giữ (kẹp 0-1000); hỏng/quá nhỏ/thiếu -> None; tay phải mượn tay trái
    jb = {"boxes": {"bodice_back": [100, 50, 900, 480], "skirt_back": [0, 0, 100, 100],
                    "top_front": [-5, 0, 300, 1200], "sl_front": [0, 0, 500, 500], "bodice_front": "x"}}
    assert _box(jb, "bodice_back") == [100, 50, 900, 480]
    assert _box(jb, "skirt_back") is None and _box(jb, "bodice_front") is None   # quá nhỏ / hỏng
    assert _box(jb, "top_front") == [0, 0, 300, 1000] and _box({}, "top_front") is None   # kẹp biên
    assert _box(jb, "sl_front") is None and _box(jb, "sr_front") is None          # tay áo: không cắt
    # ảnh nhỏ -> phóng lên MIN_SIDE; ảnh đủ lớn giữ nguyên
    tdu = Path(tempfile.mkdtemp()); Image.new("RGB", (290, 445)).save(tdu / "s.png")
    assert max(Image.open(_upscaled(tdu / "s.png", tdu / "u.png")).size) == MIN_SIDE
    Image.new("RGB", (1254, 1254)).save(tdu / "b.png")
    assert Image.open(_upscaled(tdu / "b.png", tdu / "v.png")).size == (1254, 1254)
    plan_b = _pieces_plan({"type": "dress", "bodice_front": "base green", "bodice_back": "base green",
                           "skirt_front": "base purple", "skirt_back": "base purple", **jb})
    assert {s_["slug"]: s_["box"] for s_ in plan_b}["than_sau"] == [100, 50, 900, 480]
    # _make_piece có box -> ref 1 = crop đúng vùng, ảnh nguyên ở CUỐI + CROP_NOTE; không box -> như cũ
    global gen_panel
    orig_gen, seen = gen_panel, []
    def fake_gen(prompt, refs, out_):
        seen.append((prompt, list(refs))); Image.new("RGB", (64, 64), "grey").save(out_)
    gen_panel = fake_gen
    try:
        td = Path(tempfile.mkdtemp()); src = td / "full.png"; Image.new("RGB", (1000, 800), "white").save(src)
        spec = dict(slug="ao_sau", kind="bleed", desc="base green", side="back",
                    label="x", box=[100, 100, 600, 500])
        _make_piece(spec, {"front": src, "back": src}, td, td / "o.png")
        pr, rf = seen[-1]
        assert Image.open(rf[0]).size == (500, 320) and rf[-1] == src and CROP_NOTE in pr
        _make_piece({**spec, "box": None}, {"front": src, "back": src}, td, td / "o.png")
        assert seen[-1][1] == [src] and CROP_NOTE not in seen[-1][0]
    finally:
        gen_panel = orig_gen
    assert _strip_ties("base purple; torn openings, lower left; dark green tied sash motif, right hip") \
        == "base purple; torn openings, lower left"
    print("selfcheck OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--front")
    ap.add_argument("--back", help="ảnh mặt sau riêng (pieces: dùng lưng thật)")
    ap.add_argument("--combined", action="store_true",
                    help="pieces: ảnh --front chứa CẢ mặt trước + sau (2-view)")
    ap.add_argument("--pieces", action="store_true",
                    help="tách từng mảnh (auto váy/áo-quần), full-bleed, tách người, tự suy mặt sau")
    ap.add_argument("--art", action="store_true",
                    help="1 bản vẽ phẳng nguyên bộ mặt trước; tách từng người")
    ap.add_argument("--only", help="pieces: CHỈ gen các slug này (vd ao_truoc,quan_sau)")
    ap.add_argument("--cache", help="pieces: nạp lại mô tả vision cũ (pieces.json), BỎ vision -> gen lẻ")
    ap.add_argument("--flat", action="store_true",
                    help="pieces: vẽ VECTOR phẳng (bỏ hiệu ứng 3D); mặc định giữ chi tiết/nổi 3D")
    ap.add_argument("-o", "--out", default="flat_out.png")
    ap.add_argument("--log", help="file ghi log (mặc định: <out>.log cạnh output)")
    ap.add_argument("--emit", help="ghi crop/mảnh vào thư mục này (cho UI web poll)")
    ap.add_argument("--single", action="store_true",
                    help="coi --front là 1 bộ đồ, bỏ bước tách người (batch đã tự crop)")
    ap.add_argument("--color", help="ép màu nền vải (vd 'heliconia pink'), giữ họa tiết")
    ap.add_argument("--hint", help="ghi chú sửa lỗi ghép cuối mọi prompt gen")
    ap.add_argument("--garment", choices=["top", "bottom", "top_bottom", "dress"],
                    help="ép loại SP thật (biết trước từ SKU, vd 3D Tshirt -> top); mặc định để vision tự quyết")
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args()
    if a.selfcheck:
        _selfcheck()
    elif a.front:
        if a.emit:
            globals()["EMIT"] = a.emit
        globals()["COLOR"], globals()["HINT"], globals()["GARMENT"] = a.color, a.hint, a.garment
        out = Path(a.out)
        logpath = _open_log(a.log or out.with_suffix(".log"))
        log("start", f"log -> {logpath}")
        if a.art:
            run_art(a.front, out, single=a.single)
        else:                                   # mặc định: pieces
            run_pieces(a.front, out, back=a.back, combined=a.combined, only=a.only, cache=a.cache,
                       flat=a.flat, single=a.single)
    else:
        ap.error("cần --front (+ --pieces hoặc --art), hoặc --selfcheck")
