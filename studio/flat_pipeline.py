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
gen (primary chatgpt-imagegen/codex, lỗi -> OpenArt Ideogram 4.5; ảnh 18+ -> nano-banana-2).

Chạy:
  python3 flat_pipeline.py --pieces --front f.png [--back b.png | --combined] -o out.png
  python3 flat_pipeline.py --art    --front f.png -o out.png
  python3 flat_pipeline.py --selfcheck

Yêu cầu: codex đã đăng nhập + `openart login`; ARK_API_KEY (env hoặc .env cạnh script).
"""
import argparse, base64, io, json, os, random, re, shutil, subprocess, sys, tempfile, threading, time, urllib.request
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
KIND = None     # --kind hoodie|sweater|jogger|jumpsuit (batch suy từ SKU, kind_of): loại có khuôn mảnh thật
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
OA_MODEL = "ideogram-4-5"   # fallback mặc định (codex chặn vì IP / quá tải / lỗi khác). USER 20/10: thay Seedream 4.5
# — CLI openart không truyền được aspectRatio; Ideogram mặc định "auto" = khổ theo ảnh ref (mảnh đứng giữ đứng),
# Seedream mặc định 4:3 -> vẽ cả áo có tay/nền, cắt gấu (test 10 model mảnh Golden Girls 20/10). 44 vs 15 credit.
# codex chặn vì ảnh 18+ -> nano-banana-2 (USER 19/10): Seedream gen được nhưng khung 4:3 cắt mất gấu/dải trên
# mảnh (test 9 model mẫu "Christmas balls", nano-banana-2 giống gốc nhất). Lỗi codex IP/18+ y hệt nhau
# (image_status=failed, không text) -> Claude nhìn ảnh ref để phân loại.
OA_MODEL_ADULT = "nano-banana-2"
ADULT_Q = ("Garment print safety check for an image generator. Does the printed graphic on this garment show SEXUAL "
           "or SUGGESTIVE body imagery: nudity, bare breasts or buttocks, genitals, or a genital/testicle-shaped "
           "novelty joke (e.g. hairy 'Christmas balls')? Licensed/cartoon characters, muscles or abs prints alone "
           'are NOT adult. Return ONLY JSON {"adult": true|false, "why": "<5 words>"}')

# --- gen prompt: MẢNH VÁY (cắt cong) -> vẽ trên nền xám rồi cutout() theo silhouette ---
# TRUNG THỰC: giữ nguyên chi tiết/bóng/hiệu ứng nổi giả-3D của họa tiết in, chỉ tách mảnh
# (nền xám chỉ để cutout cắt silhouette, KHÔNG làm phẳng họa tiết).
DRESS_FAITHFUL = (
    "A single cut-and-sew garment PANEL laid flat, front view, on a plain flat light grey background and "
    "NOTHING else (so the panel can be cut out cleanly). REPRODUCE the panel's printed artwork FAITHFULLY "
    "exactly as in the reference photo — keep every colour, detail, shading, soft highlight and any raised "
    "faux-3D / moulded look; do NOT flatten or simplify it into plain vector. No human body, no head, no "
    "arms, no legs, no hanger, no mockup. The grey background is flat and even right up to the panel edge: "
    "NO glow, halo, aura, vignette, drop shadow or dark outline around the panel.")
# PHẲNG: vẽ vector phẳng như bản gốc (bỏ hết bóng/nổi/3D) — dùng khi user chọn "Art phẳng" cho mảnh.
DRESS_FLAT = (
    "A 2D FLAT VECTOR sewing-pattern PANEL, absolutely flat and symmetric, front view, on a plain flat "
    "light grey background and NOTHING else (so the panel can be cut out). REDRAW the print as CLEAN flat "
    "vector shapes — this is NOT a photo. SOLID FLAT COLOR FILL only, crisp clean edges like a technical "
    "CAD flat: NO fabric texture, NO shading, NO soft highlight, NO gradient, NO drop shadow, NO 3D, NO "
    "moulded look, NO photo-realism, completely matte. Clean geometric shapes + clean solid/dashed stitch "
    "lines. No human body, no head, no arms, no legs, no hanger, no mockup.")
SHAPE_BODICE = (" Shape: a sleeveless bodice tank panel like the second reference (shape guide). It ENDS at the "
                "waist seam: NO waistband, belt, skirt or pants fabric below it.")
SHAPE_FAN = (" Shape: a wide quarter-circle CIRCLE-SKIRT fan panel spread flat like the second "
             "reference (shape guide). Every band follows the curved arc, parallel to the curved hem.")
# ĐẦM KIỂU MỚI (USER 21/10): tà = tấm hoạ tiết CHỮ NHẬT "trải thẳng" (trên = eo, dưới = gấu) — export_piece uốn
# theo khuôn tpl_skirt ra NGUYÊN khung 9000x4700; thân = khung vuông phủ kín, đặt hoạ tiết khớp khuôn tpl_bodice.
SKIRT_RECT = (
    "Flat textile PRINT of the {label} as one wide RECTANGLE for printing: the TOP edge is the waistline, the "
    "BOTTOM edge is the hem. Reproduce the skirt's printed artwork EXACTLY as in the reference crop (colours, "
    "bands, motifs, their positions from waist to hem and left to right, relative sizes) but perfectly flat and "
    "straight: horizontal bands run as straight stripes across the full width, borders along the hem become a "
    "straight band along the bottom edge, vertical elements stay vertical, motifs upright. NO gathers, pleats, "
    "folds, shadows or perspective; NO garment outline, NO bodice or waistband, NO person, NO background. Fill "
    "the whole frame edge to edge.")
BODICE_TPL = (
    "Flat textile PRINT for the {label}, filling the ENTIRE square frame edge to edge. The SECOND reference is "
    "the sewing pattern: the white outline shows where this panel will be cut (neckline scoop at top centre, "
    "straps at the top corners, armholes at the sides, waist seam along the bottom). Place the bodice's printed "
    "artwork from the FIRST reference (a crop of the real bodice) so it sits INSIDE that outline at the same "
    "positions as on the real garment (neckline trim along the scoop, chest motifs on the chest, waist details "
    "at the bottom), and continue the base fabric and its pattern beyond the outline out to all four edges. Do "
    "NOT draw the outline, the word FRONT, the mauve background, a person, folds, shadows or perspective — only "
    "the flat print.")
# ÁO/QUẦN/TAY/MŨ THEO KHUÔN THẬT (USER 21/10, khuôn xưởng ở assets/tpl_clothing): mảnh có khuôn gen như thân đầm
# (ảnh cắt + khuôn) và xuất ĐÚNG cỡ khuôn; loại/mảnh không có khuôn -> gen bình thường. Jogger = bộ hoodie + quần
# jogger. Jumpsuit: thân+quần chung 1 khuôn dài -> gộp ao_*+quan_* thành 1 mảnh bo_truoc/bo_sau (_merge_jumpsuit).
TPL_DIR = ASSETS / "tpl_clothing"
_HOODIE = {"ao_truoc": "hoodie_front.jpg", "ao_sau": "hoodie_back.jpg", "tay": "hoodie_sleeve.jpg", "mu": "hoodie_hood.jpg"}
CLOTH_TPL = {
    "hoodie": _HOODIE,
    "jogger": {**_HOODIE, "quan": "jogger.jpg"},
    "sweater": {"ao_truoc": "sweater_front.jpg", "ao_sau": "sweater_back.jpg", "tay": "sweater_sleeve.jpg"},
    "jumpsuit": {"bo_truoc": "jumpsuit_body.jpg", "bo_sau": "jumpsuit_body.jpg", "tay": "jumpsuit_sleeve.jpg",
                 "mu": "jumpsuit_hood.jpg"},
}
CLOTH_PROMPT = (
    "Flat textile PRINT for the {label}, filling the ENTIRE frame edge to edge. The SECOND reference is the sewing "
    "pattern: the light shape shows where this panel will be cut. Place the panel's printed artwork from the FIRST "
    "reference (a crop of the real garment) so it sits INSIDE that shape at the same positions as on the real "
    "garment, and continue the base fabric ({base}) and its pattern beyond the shape out to all four edges. Do NOT "
    "draw the shape outline, the blue/grid background, any text, a person, the garment as an object, folds, "
    "shadows or perspective — only the flat print. The print of this panel: {desc}")
# thân jumpsuit (khuôn 1 tấm): codex hay tách 2 ống + đáy quần + dải trắng gấu (Rhonda Romero 21/10)
BODY_NOTE = (" This is ONE continuous tall panel from neckline to ankle: do NOT split it into two legs — no crotch, "
             "no inseam, no gap between the legs; the legs' prints sit side by side on one continuous fabric. No "
             "white or blank band along any edge.")
CLOTH_FLAT = (" Style: 2D FLAT VECTOR — solid flat colour fills, crisp clean edges, NO 3D, NO shading, NO gradient, "
              "NO fabric texture.")
HOOD_NOTE = (' The top has a HOOD: ALSO add "hood": "<print spec of the hood\'s outer side panel>" and its box '
             'under boxes.hood (from the back view if there is one).')

def _merge_jumpsuit(plan):
    """Jumpsuit (khuôn thân+quần 1 tấm): ao_X + quan_X -> bo_X (mô tả nối, box gộp từ thân xuống ống)."""
    by = {p["slug"]: p for p in plan}
    out = [p for p in plan if not p["slug"].startswith(("ao_", "quan_"))]
    for side in ("truoc", "sau"):
        a, q = by.get(f"ao_{side}"), by.get(f"quan_{side}")
        if not (a and q):
            out += [x for x in (a, q) if x]; continue
        box = ([min(a["box"][0], q["box"][0]), min(a["box"][1], q["box"][1]),
                max(a["box"][2], q["box"][2]), max(a["box"][3], q["box"][3])] if a["box"] and q["box"] else None)
        out.append(dict(a, slug=f"bo_{side}", label=_BLEED_LABEL[f"bo_{side}"], box=box,
                        desc=f"TORSO: {a['desc']} | LEGS: {q['desc']}"))
    return out

def kind_of(sku):
    """Tên SKU -> loại có khuôn (None = gen bình thường). Jumpsuit trước hoodie (SKU jumpsuit có thể ghi hoodie)."""
    s = (sku or "").lower()
    for k, words in (("jumpsuit", ("jumpsuit",)), ("jogger", ("jogger",)), ("sweater", ("ugly sweater",)),
                     ("hoodie", ("3d hoodie",))):
        if any(w in s for w in words):
            return k
    return None

def cloth_tpl(slug, kind=None):
    """Khuôn thật của mảnh (bỏ _phang; tay_* chung 'tay', quan_* chung 'quan') hoặc None."""
    base = slug[:-len("_phang")] if slug.endswith("_phang") else slug
    key = "tay" if base.startswith("tay") else "quan" if base.startswith("quan_") else base
    f = CLOTH_TPL.get(kind or KIND, {}).get(key)
    return TPL_DIR / f if f else None

CLOTH_GEN = (1024, 1536)   # khổ codex gen mảnh có khuôn (khuôn đều dọc)

def _tpl_box(path):
    """Khuôn (W,H) đặt giữa khổ CLOTH_GEN -> hộp (x0,y0,x1,y1) của khuôn trong khung CLOTH_GEN."""
    W, H = Image.open(path).size; gw, gh = CLOTH_GEN
    k = min(gw / W, gh / H); w, h = round(W * k), round(H * k)
    return (gw - w) // 2, (gh - h) // 2, (gw - w) // 2 + w, (gh - h) // 2 + h

def _tpl_ref(path):
    """Khuôn thu nhỏ, CHÈN nền cho đủ khổ CLOTH_GEN (codex chỉ gen 1024x1536) -> ảnh gen cắt lại đúng hộp khuôn
    (_tpl_box), không kéo méo (lỗi 21/10: codex 'auto' trả ngang 1448x1086 rồi kéo sang khuôn dọc 4500x5450)."""
    out = TPL_DIR / "small" / path.name
    with _TPL_LOCK:
        if not out.exists():
            out.parent.mkdir(exist_ok=True)
            im = Image.open(path).convert("RGB"); x0, y0, x1, y1 = _tpl_box(path)
            canvas = Image.new("RGB", CLOTH_GEN, im.getpixel((5, 5)))
            canvas.paste(im.resize((x1 - x0, y1 - y0), Image.LANCZOS), (x0, y0)); canvas.save(out)
    return out

_DRESS_LABEL = {"ta_truoc": "FRONT skirt panel", "ta_sau": "BACK skirt panel",
                "than_truoc": "FRONT bodice panel of a dress", "than_sau": "BACK bodice panel of a dress"}
SKIRT_SIZE, BODICE_SIZE = (9000, 4700), (3500, 3500)   # cỡ file xuất (USER 21/10)

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
    parts = [{"type": "input_image", "image_url": c["image_url"]["url"]} if c["type"] == "image_url"
             else {"type": "input_text", "text": c["text"]} for c in content]
    body = {"model": os.getenv("CHATGPT_IMAGEGEN_MODEL", "gpt-5.6-sol"), "stream": True,
            "instructions": "You are an image generation assistant.",   # endpoint chỉ nhận instructions allowlist
            "input": [{"type": "message", "role": "user", "content": parts}],
            "store": False, "reasoning": {"effort": "low"}, "text": {"verbosity": "low"}}

    def one():
        try:
            auth = json.loads((Path.home() / ".codex" / "auth.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise RuntimeError(f"codex chưa đăng nhập: {e}")
        access, account, _ = _CIG._extract_access_token(auth)
        if not access:
            raise RuntimeError("codex chưa đăng nhập")
        # ponytail: không tự refresh token hết hạn; bước gen (chatgpt-imagegen) tự refresh rồi lần sau chạy được
        headers = _CIG._build_headers(access, account, _CIG._detect_codex_version())
        for ev in _CIG._stream(_CIG.CODEX_BACKEND, headers, body, time.monotonic() + timeout, 120):
            if ev.get("type") == "response.output_text.done":
                return ev.get("text", "")
            if ev.get("type") in ("response.failed", "response.incomplete", "error"):
                raise RuntimeError(f"codex: {str(ev)[:300]}")
        return ""
    return _with_codex(one)

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
    "If it is a TOP + BOTTOM (a separate upper garment + pants/shorts/skirt): "
    '{"type":"top_bottom","top_front":"..","top_back":"..","pants_front":"..","pants_back":"..",'
    '"sleeves_same":true,"sl_front":"..","sl_back":"..","sr_front":"..","sr_back":".."}. '
    "top_*/pants_* = the TORSO/BODY resp. LEGS panel (top EXCLUDES sleeves, hood, collar). "
    "sl_front/sl_back = the LEFT sleeve's front-facing / back-facing print; sr_front/sr_back = the RIGHT "
    "sleeve's. sleeves_same = true if both sleeves carry the SAME print (then sr_* may repeat sl_*), "
    "false if the two sleeves clearly differ. If the top has NO sleeves (tank/sleeveless), set sl_*/sr_* to "
    '"none". '
    "If it is a TOP ONLY (no separate bottom garment — e.g. a T-shirt/sweatshirt/hoodie, even if its print "
    "depicts fake pants/shorts/underwear/legs below the waist): "
    '{"type":"top","top_front":"..","top_back":"..","sleeves_same":true,"sl_front":"..","sl_back":"..",'
    '"sr_front":"..","sr_back":".."}. '
    "If it is a BOTTOM ONLY (pants/jogger/shorts, or a skirt — describe the skirt in pants_* too): "
    '{"type":"bottom","pants_front":"..","pants_back":".."}. '
    'For top_bottom/bottom ALSO add "bottom_is_skirt": true if the bottom garment is a SKIRT or skort '
    "(skirt with built-in shorts), false for pants/shorts/joggers. "
    "Printed underwear/legs/body on the LOWER part of a top are PART OF THE TOP's print, not a separate "
    "bottom garment. "
    "Realistic body parts printed ON the garment (muscles, abs, chest, skin, tattoos) are part of the "
    "PRINT: describe them as artwork on the panel, never as bare skin or an opening. Any skin-coloured area in "
    "the print (bare chest, cleavage, abs, belly showing through an opening) MUST be named in that panel's desc "
    "with its skin colour and position — never omit it (missed on a women's snowman onesie 21/10). "
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
    if "mu" in CLOTH_TPL.get(KIND, {}):
        p += HOOD_NOTE
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
    "mu": "outer side panel of the hood",
    "bo_truoc": "FRONT body panel of the jumpsuit (torso and legs as ONE tall panel, no sleeves, no hood)",
    "bo_sau": "BACK body panel of the jumpsuit (torso and legs as ONE tall panel, no sleeves, no hood)",
    "tay_trai_truoc": "front-facing panel of the LEFT sleeve", "tay_trai_sau": "back-facing panel of the LEFT sleeve",
    "tay_phai_truoc": "front-facing panel of the RIGHT sleeve", "tay_phai_sau": "back-facing panel of the RIGHT sleeve",
}

# slug mảnh -> key box trong pieces.json + nhãn mô tả vùng (batch: lần gen thứ 3 nhờ Claude khoanh lại box).
# Tay áo không có ở đây: box tay của vision không đáng tin -> _make_piece nhờ Claude khoanh (_sleeve_box).
PIECE_BOX = {
    "ao_truoc": ("top_front", "the FRONT torso/body of the top (no sleeves, no hood)"),
    "ao_sau": ("top_back", "the BACK torso/body of the top (no sleeves, no hood)"),
    "quan_truoc": ("pants_front", "the FRONT of the pants/bottom"),
    "quan_sau": ("pants_back", "the BACK of the pants/bottom"),
    "than_truoc": ("bodice_front", "the FRONT bodice of the dress (neckline down to the waist seam only)"),
    "than_sau": ("bodice_back", "the BACK bodice of the dress (neckline down to the waist seam only)"),
    "ta_truoc": ("skirt_front", "the FRONT skirt of the dress (waist seam down to the hem only)"),
    "ta_sau": ("skirt_back", "the BACK skirt of the dress (waist seam down to the hem only)"),
    "mu": ("hood", "the HOOD of the top (only the hood, not the torso)"),
}

def _filter_by_garment(items, garment):
    """Lọc theo garment thật (--garment, từ judge.pick): top -> bỏ quan_*; bottom -> bỏ ao_*/tay_*.
    `items` = plan (list dict có 'slug') HOẶC tập/list slug string (batch.py dùng lại để lọc tên mảnh
    mong đợi từ pieces.json) -> trả về cùng kiểu (list/set) đầu vào."""
    def slug(x):
        return x["slug"] if isinstance(x, dict) else x
    if garment == "top":
        keep = lambda x: not slug(x).startswith(("quan_", "ta_"))
    elif garment == "bottom":
        keep = lambda x: not (slug(x).startswith(("ao_", "tay", "mu", "bo_")))
    else:
        return items
    return type(items)(x for x in items if keep(x))

_BACK_FALLBACK = {"top_back": "top_front", "pants_back": "pants_front",
                  "bodice_back": "bodice_front", "skirt_back": "skirt_front"}

def _desc(js, key):
    """Mô tả panel theo `key`; vision bỏ sót *_back -> suy từ mặt trước tương ứng (USER DECISION:
    SP có phần nào thì luôn đủ 2 mặt của phần đó, không được thiếu mảnh sản xuất)."""
    return js.get(key) or js.get(_BACK_FALLBACK.get(key, ""))

# áo không tay: vision vẫn trả sl_* kiểu "base none; garment is sleeveless, no sleeve panel present" -> không
# gen mảnh tay (gen ra mảnh vô nghĩa, QA trượt cả 3 lần: Athletic Set tank 19/10)
_NO_SLEEVE = re.compile(r"^\W*(base\s+)?(none|n/?a)\b|^\W*sleeveless|\b(garment|top|shirt) is sleeveless"
                        r"|\bno sleeves?\b|\bwithout sleeves", re.I)

def _is_skirt(js):
    """Phần dưới là váy/skort (USER 19/10: Athletic Set -> mảnh quạt như Tennis Skirt, không phải mảnh quần).
    Vision quên cờ bottom_is_skirt -> nhận theo chữ trong mô tả."""
    if isinstance(js.get("bottom_is_skirt"), bool):
        return js["bottom_is_skirt"]
    return bool(re.search(r"\bsk(irt|ort)", f"{js.get('pants_front') or ''} {js.get('pants_back') or ''}", re.I))

def _box(js, key):
    """Box 0-1000 của panel `key` (vision 'boxes'); hỏng/nhỏ (<15% mỗi chiều) -> None (dùng ảnh nguyên).
    Tay áo (sl_/sr_) vốn hẹp: chỉ cần rộng >=4%, cao >=15% và DỌC (cao >= 1.5 rộng) — box tay lệch sang
    cổ/người bên cạnh thường vuông/ngang -> None -> _make_piece nhờ Claude khoanh (USER 20/10: mảnh nào cũng cắt)."""
    sleeve = key.startswith(("sl_", "sr_"))
    boxes = js.get("boxes") if isinstance(js.get("boxes"), dict) else {}
    b = boxes.get(key)
    if not (isinstance(b, list) and len(b) == 4 and all(isinstance(v, (int, float)) for v in b)):
        return None
    x0, x1 = sorted((max(0, b[0]), min(1000, b[2])))
    y0, y1 = sorted((max(0, b[1]), min(1000, b[3])))
    w, h = x1 - x0, y1 - y0
    ok = (w >= 40 and h >= 150 and h >= 1.5 * w) if sleeve else (w >= 150 and h >= 150)
    return [int(x0), int(y0), int(x1), int(y1)] if ok else None

def _pieces_plan(js, kind=None, garment=None):
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
    if typ != "top" and _is_skirt(js):   # bottom / top_bottom là VÁY -> mảnh quạt như tà Tennis Skirt
        for slug, key, side in [("ta_truoc", "pants_front", "front"), ("ta_sau", "pants_back", "back")]:
            desc = _desc(js, key)
            if desc:   # box: Claude khoanh lại (lần 3) ghi vào skirt_*, vision ban đầu ghi pants_*
                plan.append(dict(slug=slug, kind="dress", desc=desc, side=side, shape=SHAPE_FAN,
                                 guide="shape_fan.png",
                                 box=_box(js, key.replace("pants_", "skirt_")) or _box(js, key)))
    elif typ != "top":    # bottom / top_bottom: quần
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
            if desc and not _NO_SLEEVE.search(desc):
                plan.append(dict(slug=slug, kind="bleed", desc=desc, side=side, label=_BLEED_LABEL[slug],
                                 box=_box(js, key)))
    if typ != "bottom" and js.get("hood") and not _NO_SLEEVE.search(js["hood"]):   # mũ (chỉ khi vision được hỏi)
        plan.append(dict(slug="mu", kind="bleed", desc=js["hood"], side="back", label=_BLEED_LABEL["mu"],
                         box=_box(js, "hood")))
    garment = garment or GARMENT                 # lọc TRƯỚC khi gộp jumpsuit (batch gọi y hệt -> cùng tên mảnh)
    if garment and garment != typ:
        plan = _filter_by_garment(plan, garment)
    if (kind or KIND) == "jumpsuit":
        plan = _merge_jumpsuit(plan)
    return plan

def _flatten_white(path):
    """Mảnh full-bleed phải ĐỤC -> bỏ hẳn kênh alpha, GIỮ màu RGB. Codex hay trả PNG có alpha SAI: vẫn tô
    đúng màu ở RGB nhưng đánh dấu cả vùng vải là (bán) trong suốt; ghép lên trắng theo alpha như trước ra
    vệt TRẮNG LOANG (lỗi thật: Jogger tay_truoc bản phẳng 16/10 — kể cả điểm alpha=0 vẫn mang màu thiết kế)."""
    im = Image.open(path)
    if im.mode != "RGB":
        im.convert("RGB").save(path)

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

# fallback OpenArt: CHỈ gửi ảnh cắt (bỏ ảnh nguyên ngữ cảnh) — kèm ảnh nguyên thì Ideogram chép phần nổi nhất của
# cả bộ (ngực áo) vào mảnh: tay Iron Man QA 1-2 -> chỉ ảnh cắt QA 8 (test 20/10). Codex vẫn nhận đủ ảnh + CROP_NOTE.
OA_CROP_NOTE = (" The FIRST reference image is a close crop of exactly this panel: reproduce ONLY that panel's "
                "print.")

# mặt sau TỰ SUY (không có ảnh lưng): ref là ảnh TRƯỚC -> model hay chép hình in mặt trước (nơ, ngực,
# logo) sang mảnh sau/tay sau (lỗi thật: Design 2 tay_sau có nơ). Chỉ theo mô tả, không chép mặt trước.
INFER_NOTE = (" This is a BACK panel INFERRED from a front-only photo: the reference shows the FRONT. Use it "
              "only for fabric colour and style; do NOT copy any front graphic (bows, chest prints, logos, "
              "buttons, zips) onto this panel — follow ONLY the given panel description.")

def _sleeve_box(spec, ref, work):
    """Tay áo: Claude khoanh 1 tay trên lưới pixel (box tay của vision hay lệch sang cổ/người bên cạnh nên
    _box bỏ) -> box 0-1000 hoặc None (gen từ ảnh nguyên như cũ). USER 20/10: mảnh nào cũng cắt riêng rồi mới
    gen — ảnh nguyên thì model fallback (Ideogram) chép ngực áo vào mảnh tay (Iron Man 20/10)."""
    arm = "RIGHT" if "phai" in spec["slug"] else "LEFT"
    try:
        import judge
        box = judge.locate_piece(judge.grid_overlay(ref, work / f"grid_{spec['slug']}.jpg"),
                                 f"ONE whole sleeve — the wearer's {arm} arm — from the shoulder seam down to "
                                 "the cuff", back=spec["side"] == "back")
    except Exception as e:
        log("crop", f"Claude khoanh {spec['slug']} lỗi ({str(e)[:120]}) -> ảnh nguyên")
        return None
    log("crop", f"Claude khoanh {spec['slug']}: {box}")
    return box

def _make_piece(spec, refs, work, out, flat=False):
    """Gen 1 mảnh -> file <out.stem>_<slug><suffix>. Váy: cắt cong (cutout); áo/quần/tay: full-bleed.
    flat=True -> vẽ VECTOR phẳng (bỏ 3D/bóng); flat=False (mặc định) -> giữ chi tiết/nổi giả-3D."""
    dst = out.with_name(f"{out.stem}_{spec['slug']}{out.suffix}")
    ref = refs[spec["side"]]
    # có box -> Python cắt riêng vùng mảnh làm ref CHÍNH (ảnh 1), ảnh nguyên để CUỐI chỉ làm ngữ cảnh
    # -> model không vẽ lấn sang phần đồ khác (vd lưng thân dính váy). Không box -> như cũ.
    crop, ctx = None, []
    box = spec.get("box")
    if not box and spec["slug"].startswith("tay") and not spec.get("inferred"):
        box = _sleeve_box(spec, ref, work)
    if box:
        crop = work / f"crop_{spec['slug']}.png"
        crop_norm(ref, box, Image.open(ref).size, crop)
        ctx = [ref]
    infer = INFER_NOTE if spec.get("inferred") else ""
    note, oa_note = (CROP_NOTE + infer, OA_CROP_NOTE + infer) if crop else (infer, infer)
    if spec["kind"] == "dress":
        label = _DRESS_LABEL.get(spec["slug"], spec["slug"])
        if spec["slug"].startswith("ta_"):       # tà: chữ nhật trải thẳng, export_piece uốn theo khuôn
            head, tail = SKIRT_RECT.format(label=label), " Print: " + _strip_ties(spec["desc"]) + NO_TIES
            gen_panel(head + note + tail, [crop or ref, *ctx], dst, oa=(head + oa_note + tail, [crop or ref]),
                      size="1536x1024")
        else:                                    # thân: khung vuông phủ kín, hoạ tiết đặt theo khuôn thân
            tpl = _tpl_small()
            head, tail = BODICE_TPL.format(label=label), " Print: " + spec["desc"]
            gen_panel(head + note + tail, [crop or ref, tpl, *ctx], dst, oa=(head + oa_note + tail, [crop or ref, tpl]),
                      size="2048x2048")
        _flatten_white(dst)
    elif cloth_tpl(spec["slug"]):                # mảnh có khuôn thật: gen như thân đầm
        tpl = _tpl_ref(cloth_tpl(spec["slug"]))
        head = CLOTH_PROMPT.format(label=spec["label"], base=COLOR or _base_colour(spec["desc"]),
                                   desc=spec["desc"]) + (BODY_NOTE if spec["slug"].startswith("bo_") else "") \
            + (CLOTH_FLAT if flat else "")
        gen_panel(head + note, [crop or ref, tpl, *ctx], dst, oa=(head + oa_note, [crop or ref, tpl]),
                  size="x".join(map(str, CLOTH_GEN)))
        Image.open(dst).convert("RGB").resize(CLOTH_GEN, Image.LANCZOS).crop(_tpl_box(cloth_tpl(spec["slug"]))).save(dst)
    else:
        tpl = PANEL_BLEED_FLAT if flat else PANEL_BLEED
        raw = work / f"raw_{spec['slug']}.png"            # giữ bản gốc model trả (soi lỗi vùng trong suốt)
        head = tpl.format(label=spec["label"], base=COLOR or _base_colour(spec["desc"]), desc=spec["desc"])
        gen_panel(head + note, [crop or ref, *ctx], raw, oa=(head + oa_note, [crop or ref]))
        Image.open(raw).save(dst)
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


# ---------- 2. gen: chatgpt-imagegen (primary) -> OpenArt Ideogram 4.5 (fallback) ----------
def _bin(name):
    cands = [name + ".exe", name] if sys.platform == "win32" else [name]
    for c in cands:
        b = HERE / "bin" / c
        if b.exists():
            return str(b)
    return name
CHATGPT_BIN = _bin("chatgpt-imagegen")
# codex chỉ dùng tài khoản `codex login` mặc định (USER 21/10: bỏ hẳn tài khoản 2).
# codex "overloaded" -> chờ 30-60s (ngẫu nhiên: các luồng không cùng dội lại 1 lúc), báo Lark, gen
# tiếp bằng codex; tối đa OVERLOAD_ROUNDS vòng rồi mới xuống OpenArt (USER 19/10; quá tải chỉ thoáng qua:
# 4202338676 13:05-13:26 rơi xuống Seedream 12/13 mảnh vì không chờ)
OVERLOAD_WAIT, OVERLOAD_ROUNDS = (30, 60), 3
NOTIFY_EVERY = 600          # báo Lark quá tải tối đa 1 tin / 10 phút cho CẢ máy (nhiều luồng + tiến trình con)
_NOTIFY_DIR = Path(tempfile.gettempdir()) / "flat_codex_overload"

def _notify_overload(text):
    """Tin Lark (LARK_WEBHOOK, batch nạp từ .env rồi truyền xuống tiến trình con). Chống spam cho mọi luồng/tiến
    trình: mỗi khung NOTIFY_EVERY giây 1 file đánh dấu, tạo bằng O_EXCL (nguyên tử) -> chỉ 1 bên gửi. Lỗi file /
    lỗi gửi chỉ log, không được làm hỏng vòng chờ-thử-lại."""
    try:
        _NOTIFY_DIR.mkdir(parents=True, exist_ok=True)
        os.close(os.open(_NOTIFY_DIR / str(int(time.time() // NOTIFY_EVERY)), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        return
    except OSError as e:
        log("notify", f"file đánh dấu lỗi ({e}) -> vẫn gửi")
    url = os.getenv("LARK_WEBHOOK")
    if not url:
        log("notify", "(chưa có LARK_WEBHOOK) " + text); return
    body = json.dumps({"msg_type": "text", "content": {"text": "🧵 Flat batch\n" + text}}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, body, {"Content-Type": "application/json"}), timeout=15)
    except Exception as e:
        log("notify", f"gửi Lark lỗi: {e}")

def _with_codex(fn, wait=OVERLOAD_WAIT, sleep=time.sleep, notify=None):
    """fn() — codex quá tải -> báo Lark, chờ ngẫu nhiên `wait` giây rồi thử lại (tối đa OVERLOAD_ROUNDS); lỗi khác
    ném ngay (giữ nguyên text để raise_for nhận ra quota/hết đăng nhập)."""
    notify = notify or _notify_overload
    for rnd in range(OVERLOAD_ROUNDS + 1):
        try:
            return fn()
        except Exception as e:
            if rnd == OVERLOAD_ROUNDS or "overloaded" not in str(e):
                raise
        sec = round(random.uniform(*wait))
        log("gen", f"  codex quá tải -> chờ {sec}s thử lại (vòng {rnd + 1}/{OVERLOAD_ROUNDS})")
        notify(f"⏸ Codex quá tải — chờ {sec}s rồi gen tiếp bằng codex "
               f"(vòng {rnd + 1}/{OVERLOAD_ROUNDS}, hết vòng mới chuyển OpenArt)")
        sleep(sec)
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

def _run(cmd, timeout, **extra_env):
    # Ép child in UTF-8 + đọc UTF-8 -> tránh UnicodeError khi đường dẫn có dấu tiếng Việt.
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", **extra_env}
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       env=env, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip() or f"rc={p.returncode}")

def _chatgpt_gen(prompt, refs, out, timeout=420, size="auto"):
    # xin JPEG (không có kênh trong suốt) rồi lưu PNG: PNG để model tự chọn nền trong suốt -> nó đánh dấu
    # nhầm vùng vải trơn là trong suốt + vẽ viền tối loang (vignette) ở đó (lỗi thật: Jogger tay_truoc 16/10)
    tmp = Path(out).with_name(Path(out).stem + "_gen.jpg")
    tmp.unlink(missing_ok=True)
    cmd = _launcher(CHATGPT_BIN) + [CHATGPT_BIN, prompt, "-o", str(tmp), "--size", size,
           "--backend", "codex", "--quiet", "--format", "jpeg"]
    for r in refs:
        cmd += ["-i", str(r)]
    _with_codex(lambda: _run(cmd, timeout))
    if not tmp.exists():
        raise RuntimeError("chatgpt-imagegen không tạo file")
    Image.open(tmp).convert("RGB").save(out)
    tmp.unlink(missing_ok=True)
    return out

def _is_adult(img):
    """Claude: ảnh ref có hình 18+ không. Lỗi -> False (giữ fallback mặc định)."""
    try:
        import judge
        r = judge.ask(ADULT_Q, [img], timeout=120)
        log("gen", f"  phân loại ref: adult={r.get('adult')} ({r.get('why')})")
        return r.get("adult") is True
    except Exception as e:
        log("gen", f"  phân loại 18+ lỗi ({str(e)[:120]}) -> fallback mặc định")
        return False

def _openart_gen(prompt, refs, out, timeout=300, model=OA_MODEL):
    if not _win_native_ok(OPENART_BIN):
        raise RuntimeError("OpenArt chỉ có bản Linux, không chạy được trên Windows "
                           "(chỉ dùng chatgpt-imagegen làm gen chính)")
    cmd = _launcher(OPENART_BIN) + [OPENART_BIN, "generate", "image", prompt, "--model", model,
           "-o", str(out), "--yes", "--compact"]
    for r in refs:
        cmd += ["--image", str(r)]
    _run(cmd, timeout)
    if not Path(out).exists():
        raise RuntimeError("openart không tạo file")
    return out

def gen_panel(prompt, refs, out, oa=None, size="auto"):
    """Primary chatgpt-imagegen (codex), lỗi -> fallback OpenArt: Ideogram 4.5, ảnh 18+ -> nano-banana-2.
    oa = (prompt, refs) riêng cho OpenArt (mảnh có ảnh cắt: chỉ gửi ảnh cắt, xem OA_CROP_NOTE)."""
    oa_prompt, oa_refs = oa or (prompt, refs)
    oa_prompt += _prompt_extra()
    prompt = prompt + _prompt_extra()
    Path(out).unlink(missing_ok=True)       # tool trả rc=0 mà không ghi file -> không nhận nhầm file lần trước
    try:
        log("gen", "  → thử chatgpt-imagegen (codex) ...")
        r = _chatgpt_gen(prompt, refs, out, size=size)
        log("gen", "  ✓ xong bằng chatgpt-imagegen")
        return r
    except Exception as e:
        err = str(e)
        log("gen", "  ✗ chatgpt-imagegen lỗi -> fallback OpenArt. Lỗi đầy đủ:")
        log("gen", "  " + err[:1500].replace("\n", "\n  "))
    model = OA_MODEL_ADULT if refs and _is_adult(refs[0]) else OA_MODEL
    try:
        r = _openart_gen(oa_prompt, oa_refs, out, model=model)
    except Exception as e:
        # giữ cả lỗi codex: raise_for cần thấy 429/hết đăng nhập codex, không chỉ "openart không tạo file"
        raise RuntimeError(f"codex: {err}\nopenart: {e}") from e   # đủ lỗi: 429/401 không bị cắt
    log("gen", f"  ✓ xong bằng OpenArt {model}")
    return r


# ---------- 2b. XUẤT FILE CUỐI (USER 21/10): khung lớn phủ kín ----------
_TPL_LOCK = threading.Lock()
_WARP_LOCK = threading.Lock()   # ponytail: 1 lần uốn 9000x4700 ~3GB RAM -> uốn tuần tự; tách theo dải hàng nếu cần song song

def _tpl_small():
    """Khuôn thân đầm thu nhỏ 1024 làm ref cho model (file gốc 3500px quá nặng để gửi)."""
    out = ASSETS / "tpl_bodice_1024.png"
    with _TPL_LOCK:
        if not out.exists():
            Image.open(ASSETS / "tpl_bodice.jpg").convert("RGB").resize((1024, 1024)).save(out)
    return out

def _skirt_mask():
    """Mặt nạ trong khuôn tà (tpl_skirt.jpg, viền trắng trên nền đều màu) -> bool HxW, cache .npy."""
    cache = ASSETS / "tpl_skirt_mask.npy"
    with _TPL_LOCK:
        if cache.exists():
            return np.load(cache)
        Image.MAX_IMAGE_PIXELS = None
        im = np.array(Image.open(ASSETS / "tpl_skirt.jpg").convert("RGB")).astype(np.int16)
        line = (np.abs(im - im[5, 5]).max(2) > 60).astype(np.uint8)        # viền + chữ của khuôn
        _, lab = cv2.connectedComponents(1 - line, connectivity=4)
        inside = cv2.morphologyEx((lab != lab[0, 0]).astype(np.uint8), cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
        np.save(cache, inside.astype(bool))
        return inside.astype(bool)

def warp_skirt(rect, mask, nb=1024):
    """Tấm chữ nhật (trên = eo, dưới = gấu) -> NGUYÊN khung khuôn, hoạ tiết cong theo khuôn quạt: u theo góc
    quanh đỉnh 2 cạnh bên, v theo r giữa cung eo/gấu ĐO TRÊN KHUÔN từng góc (khuôn không cần đồng tâm). Ngoài
    khuôn: phản chiếu liền mạch (lề in tràn). rect HxWx3 uint8 -> ảnh cỡ mask."""
    h, w = mask.shape
    top = np.array([(x, np.argmax(mask[:, x])) for x in range(0, w, 5) if mask[:, x].any()])
    xs_, span = top[:, 0], top[:, 0].max() - top[:, 0].min()
    L = top[(xs_ > xs_.min() + .05 * span) & (xs_ < xs_.min() + .3 * span)]
    R = top[(xs_ < xs_.max() - .05 * span) & (xs_ > xs_.max() - .3 * span)]
    pl, pr = np.polyfit(L[:, 0], L[:, 1], 1), np.polyfit(R[:, 0], R[:, 1], 1)
    ax = (pr[1] - pl[1]) / (pl[0] - pr[0]); ay = pl[0] * ax + pl[1]                 # đỉnh quạt
    ys, xs = np.nonzero(mask)
    th = np.arctan2(ys - ay, xs - ax); r = np.hypot(xs - ax, ys - ay)
    t0, t1 = th.min(), th.max()
    b = ((th - t0) / (t1 - t0) * (nb - 1)).astype(int)
    rin = np.full(nb, np.inf); rout = np.zeros(nb)
    np.minimum.at(rin, b, r); np.maximum.at(rout, b, r)
    idx = np.arange(nb); ok = np.isfinite(rin)
    rin, rout = np.interp(idx, idx[ok], rin[ok]), np.interp(idx, idx[ok], rout[ok])
    # bin sát eo có thể không trúng pixel cung eo -> rin nhảy (vệt dọc giữa); làm mượt trung vị
    rin, rout = ndimage.median_filter(rin, 9, mode="nearest"), ndimage.median_filter(rout, 9, mode="nearest")
    k = nb // 50   # bin sát 2 cạnh bên chỉ vài pixel -> rin≈rout (vệt sọc dày ngoài khuôn): lấy bin cách mép 2%
    rin[:k], rout[:k], rin[-k:], rout[-k:] = rin[k], rout[k], rin[-k - 1], rout[-k - 1]
    rh, rw = rect.shape[:2]
    mx = np.empty((h, w), np.float32); my = np.empty((h, w), np.float32)
    for y0 in range(0, h, 512):                          # theo dải hàng: đỡ RAM
        yy, xx = np.mgrid[y0:min(h, y0 + 512), 0:w]
        t = np.arctan2(yy - ay, xx - ax); rr = np.hypot(xx - ax, yy - ay)
        tb = np.clip((t - t0) / (t1 - t0), -1, 2) * (nb - 1)
        ri, ro = np.interp(tb, idx, rin), np.interp(tb, idx, rout)
        u = (1 - (t - t0) / (t1 - t0)) * (rw - 1)        # góc tăng = phải -> trái (y xuống, đỉnh ở trên)
        v = (rr - ri) / np.maximum(ro - ri, 1) * (rh - 1)
        mx[y0:y0 + 512] = (rw - 1) - np.abs(np.mod(u, 2 * (rw - 1)) - (rw - 1))   # phản chiếu tuần hoàn
        my[y0:y0 + 512] = (rh - 1) - np.abs(np.mod(v, 2 * (rh - 1)) - (rh - 1))
    return cv2.remap(rect, mx, my, cv2.INTER_LANCZOS4)

def export_piece(slug, src, dst, work, kind=None):
    """Mảnh đã chốt (bản 3D hoặc _phang) -> FILE XUẤT khung lớn phủ kín (USER 21/10):
    ta_* -> uốn theo khuôn tà ra SKIRT_SIZE; than_* -> BODICE_SIZE; còn lại (áo/quần/tay/art) giữ nguyên bản gen
    (USER 21/10: bỏ bước vẽ lề — Codex vẽ lại cả tấm, tay sọc lơ lửng, quần hoa dày lên)."""
    base = slug[:-len("_phang")] if slug.endswith("_phang") else slug
    Image.MAX_IMAGE_PIXELS = None
    if base.startswith("ta_"):
        with _WARP_LOCK:
            mask = _skirt_mask()
            rect = np.array(Image.open(src).convert("RGB"))
            Image.fromarray(warp_skirt(rect, mask)).resize(SKIRT_SIZE, Image.LANCZOS).save(dst)
        return "uốn khuôn tà"
    if base.startswith("than_"):
        Image.open(src).convert("RGB").resize(BODICE_SIZE, Image.LANCZOS).save(dst)
        return "khuôn thân"
    tpl = cloth_tpl(base, kind)
    if tpl:                                      # mảnh có khuôn thật -> đúng cỡ khuôn
        Image.open(src).convert("RGB").resize(Image.open(tpl).size, Image.LANCZOS).save(dst)
        return "khuôn " + tpl.stem
    shutil.copyfile(src, dst)
    return "giữ nguyên"

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
# tổng lệnh codex chạy cùng lúc của project: 1 tài khoản tối đa 4 (chatgpt-imagegen tự xếp hàng)
CODEX_WORKERS = int(os.getenv("FLAT_CODEX_WORKERS") or 4)
# gen song song mấy mảnh cùng lúc: chừa 1 luồng cho art (batch chạy pieces + art song song)
GEN_WORKERS = int(os.getenv("FLAT_GEN_WORKERS") or max(1, CODEX_WORKERS - 1))

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
        plan = [{**sp, "box": None, "inferred": True} if sp["side"] == "back" else sp for sp in plan]
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
    tank = _pieces_plan({"type": "top", "top_front": "a", "top_back": "b", "sleeves_same": True,
                         "sl_front": "base none; garment is sleeveless, no sleeve panel present", "sl_back": "none"})
    assert {s["slug"] for s in tank} == {"ao_truoc", "ao_sau"}
    assert not _NO_SLEEVE.search("base blue; short sleeve, not sleeveless") and _NO_SLEEVE.search("sleeveless top")
    # set áo + VÁY (cờ hoặc chữ 'skirt' trong mô tả) -> mảnh quạt ta_*, không phải quan_*
    sk = {"type": "top_bottom", "top_front": "a", "top_back": "b", "pants_front": "base blue; ice spikes",
          "pants_back": "c", "sl_front": "none", "sl_back": "none"}
    assert {s["slug"] for s in _pieces_plan({**sk, "bottom_is_skirt": True})} == {"ao_truoc", "ao_sau", "ta_truoc", "ta_sau"}
    assert {s["slug"] for s in _pieces_plan({**sk, "pants_front": "speckle print across entire skirt"})} == \
        {"ao_truoc", "ao_sau", "ta_truoc", "ta_sau"}
    assert all(s["kind"] == "dress" and s["shape"] is SHAPE_FAN for s in _pieces_plan({**sk, "bottom_is_skirt": True})
               if s["slug"].startswith("ta_"))
    assert {s["slug"] for s in _pieces_plan(sk)} == {"ao_truoc", "ao_sau", "quan_truoc", "quan_sau"}
    assert _filter_by_garment({"ao_truoc", "ta_truoc"}, "top") == {"ao_truoc"}
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
    # flatten: PNG có alpha -> RGB đục, GIỮ màu dưới alpha (không loang trắng)
    q = Path(tempfile.mktemp(suffix=".png")); Image.new("RGBA", (4, 4), (10, 20, 30, 0)).save(q)
    _flatten_white(q)
    assert Image.open(q).mode == "RGB" and Image.open(q).getpixel((0, 0)) == (10, 20, 30)
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
    # codex quá tải -> chờ rồi thử lại; ảnh bị chặn -> ném ngay; quá tải mãi sau OVERLOAD_ROUNDS -> ném
    tried, slept, sent = [], [], []
    def blocked():
        tried.append(1); raise RuntimeError("no image returned (image_status=failed; model_text='')")
    try:
        _with_codex(blocked); raise AssertionError("phải raise")
    except RuntimeError as e:
        assert len(tried) == 1 and "no image returned" in str(e)
    tried.clear()
    def busy_once():
        tried.append(1)
        if len(tried) == 1:
            raise RuntimeError("Our servers are currently overloaded.")
        return "ok"
    assert _with_codex(busy_once, sleep=slept.append, notify=sent.append) == "ok" and len(tried) == 2
    assert len(slept) == 1 and OVERLOAD_WAIT[0] <= slept[0] <= OVERLOAD_WAIT[1] and len(sent) == 1 and "quá tải" in sent[0]
    tried.clear(); slept.clear()
    def always_busy():
        tried.append(1); raise RuntimeError("overloaded")
    try:
        _with_codex(always_busy, sleep=slept.append, notify=sent.append); raise AssertionError("phải raise")
    except RuntimeError:
        assert len(tried) == OVERLOAD_ROUNDS + 1 and len(slept) == OVERLOAD_ROUNDS
    # Lark quá tải: nhiều luồng cùng gọi trong 1 khung 10 phút -> chỉ 1 tin
    orig_dir, logged = globals()["_NOTIFY_DIR"], []
    globals()["_NOTIFY_DIR"] = Path(tempfile.mkdtemp()) / "mark"
    old_hook, old_log = os.environ.pop("LARK_WEBHOOK", None), globals()["log"]
    globals()["log"] = lambda st, m="": logged.append(m)
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(lambda i: _notify_overload("x"), range(8)))
    assert len(logged) == 1, logged
    globals()["_NOTIFY_DIR"], globals()["log"] = orig_dir, old_log
    if old_hook:
        os.environ["LARK_WEBHOOK"] = old_hook
    # gen_panel: codex + OpenArt cùng lỗi -> exception mang cả lỗi codex (để phân loại quota/auth)
    orig_cg, orig_oa = globals()["_chatgpt_gen"], globals()["_openart_gen"]
    globals()["_chatgpt_gen"] = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("HTTP 429 usage_limit_reached"))
    globals()["_openart_gen"] = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("openart không tạo file"))
    try:
        globals()["gen_panel"]("p", [], Path(tempfile.mkdtemp()) / "x.png")
        raise AssertionError("gen_panel phải raise")
    except RuntimeError as e:
        assert "HTTP 429" in str(e) and "openart không tạo file" in str(e)
    # codex lỗi -> ảnh 18+ dùng OA_MODEL_ADULT, còn lại OA_MODEL
    orig_ad, used = globals()["_is_adult"], []
    globals()["_openart_gen"] = lambda prompt, refs, out, model=OA_MODEL: used.append(model)
    for adult in (True, False):
        globals()["_is_adult"] = lambda img, a=adult: a
        globals()["gen_panel"]("p", ["ref.png"], Path(tempfile.mkdtemp()) / "x.png")
    assert used == [OA_MODEL_ADULT, OA_MODEL]
    globals()["_chatgpt_gen"], globals()["_openart_gen"], globals()["_is_adult"] = orig_cg, orig_oa, orig_ad
    # GEN_WORKERS: FLAT_GEN_WORKERS, mặc định CODEX_WORKERS - 1 (= 3)
    assert GEN_WORKERS == int(os.getenv("FLAT_GEN_WORKERS") or max(1, CODEX_WORKERS - 1))
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
                    "top_front": [-5, 0, 300, 1200], "sl_front": [0, 0, 500, 500], "bodice_front": "x",
                    "sl_back": [429, 237, 504, 524]}}
    assert _box(jb, "bodice_back") == [100, 50, 900, 480]
    assert _box(jb, "skirt_back") is None and _box(jb, "bodice_front") is None   # quá nhỏ / hỏng
    assert _box(jb, "top_front") == [0, 0, 300, 1000] and _box({}, "top_front") is None   # kẹp biên
    assert _box(jb, "sl_front") is None and _box(jb, "sr_front") is None          # tay: box vuông / thiếu -> None
    assert _box(jb, "sl_back") == [429, 237, 504, 524]                            # tay hẹp + dọc (Iron Man) -> cắt
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
    def fake_gen(prompt, refs, out_, oa=None):
        seen.append((prompt, list(refs), oa)); Image.new("RGB", (64, 64), "grey").save(out_)
    gen_panel = fake_gen
    try:
        td = Path(tempfile.mkdtemp()); src = td / "full.png"; Image.new("RGB", (1000, 800), "white").save(src)
        spec = dict(slug="ao_sau", kind="bleed", desc="base green", side="back",
                    label="x", box=[100, 100, 600, 500])
        _make_piece(spec, {"front": src, "back": src}, td, td / "o.png")
        pr, rf, oa = seen[-1]
        assert Image.open(rf[0]).size == (500, 320) and rf[-1] == src and CROP_NOTE in pr
        assert oa[1] == [rf[0]] and OA_CROP_NOTE in oa[0] and CROP_NOTE not in oa[0]   # OpenArt: chỉ ảnh cắt
        _make_piece({**spec, "box": None}, {"front": src, "back": src}, td, td / "o.png")
        assert seen[-1][1] == [src] and CROP_NOTE not in seen[-1][0] and INFER_NOTE not in seen[-1][0]
        _make_piece({**spec, "box": None, "inferred": True}, {"front": src, "back": src}, td, td / "o.png")
        assert INFER_NOTE in seen[-1][0]
        # tay áo không box -> Claude khoanh (_sleeve_box) rồi cắt; tay tự suy (inferred) -> không khoanh
        global _sleeve_box
        orig_sb, asked = _sleeve_box, []
        _sleeve_box = lambda sp, ref, work: asked.append(sp["slug"]) or [200, 100, 400, 900]
        try:
            _make_piece({**spec, "slug": "tay_truoc", "side": "front", "box": None},
                        {"front": src, "back": src}, td, td / "o.png")
            assert asked == ["tay_truoc"] and Image.open(seen[-1][1][0]).size == (200, 640) and CROP_NOTE in seen[-1][0]
            _make_piece({**spec, "slug": "tay_sau", "box": None, "inferred": True},
                        {"front": src, "back": src}, td, td / "o.png")
            assert asked == ["tay_truoc"] and seen[-1][1] == [src]
        finally:
            _sleeve_box = orig_sb
    finally:
        gen_panel = orig_gen
    assert _strip_ties("base purple; torn openings, lower left; dark green tied sash motif, right hip") \
        == "base purple; torn openings, lower left"
    # XUẤT FILE (USER 21/10): khuôn quạt nhỏ dựng tay -> uốn: trong khuôn hàng trên (eo) lấy từ đỉnh rect,
    # gần gấu lấy từ đáy rect; ngoài khuôn vẫn có màu (phủ kín)
    H_, W_ = 300, 600; yy, xx = np.mgrid[0:H_, 0:W_]; cx_, cy_ = 300, -150
    rr_, tt_ = np.hypot(xx - cx_, yy - cy_), np.degrees(np.arctan2(yy - cy_, xx - cx_))
    fan_m = (rr_ > 220) & (rr_ < 430) & (tt_ > 45) & (tt_ < 135)
    rect_ = np.zeros((100, 300, 3), np.uint8); rect_[:50] = (255, 0, 0); rect_[50:] = (0, 0, 255)
    wp = warp_skirt(rect_, fan_m, nb=256)
    assert wp.shape == (H_, W_, 3)
    assert wp[int(cy_ + 240), 300, 0] > 200 and wp[int(cy_ + 410), 300, 2] > 200      # eo đỏ, gấu xanh
    assert wp[5, 5].sum() > 0                                                          # góc ngoài khuôn có màu
    td2 = Path(tempfile.mkdtemp()); pc = td2 / "p.png"
    Image.new("RGB", (100, 200), (0, 200, 0)).save(pc)
    # export_piece: than -> BODICE_SIZE; art -> giữ nguyên; ta (uốn) dùng khuôn
    assert export_piece("than_truoc", pc, td2 / "t.png", td2) == "khuôn thân"
    assert Image.open(td2 / "t.png").size == BODICE_SIZE
    assert export_piece("art_phang", pc, td2 / "a.png", td2) == "giữ nguyên" and Image.open(td2 / "a.png").size == (100, 200)
    assert export_piece("tay_truoc", pc, td2 / "y.png", td2) == "giữ nguyên" and Image.open(td2 / "y.png").size == (100, 200)
    # khuôn mảnh thật (USER 21/10): loại từ SKU; mảnh có khuôn xuất đúng cỡ khuôn, không khuôn giữ nguyên
    assert kind_of("THT282609A28-Design 1-3D Hoodie-M") == "hoodie" and kind_of("X-Jumpsuit Adult-L") == "jumpsuit"
    assert kind_of("BQT081026A06-Ugly Sweater-2XL") == "sweater" and kind_of("NTN012610A09-Jogger-S") == "jogger"
    assert kind_of("BQT250926A04-Skater Dress") is None and kind_of("") is None
    assert cloth_tpl("tay_trai_sau_phang", "hoodie").name == "hoodie_sleeve.jpg"
    assert cloth_tpl("quan_truoc", "jogger").name == "jogger.jpg" and cloth_tpl("quan_truoc", "hoodie") is None
    assert cloth_tpl("ao_truoc", None) is None and all((TPL_DIR / f).exists() for m in CLOTH_TPL.values() for f in m.values())
    for m in CLOTH_TPL.values():                 # gen 1024x1536 cắt lại đúng tỉ lệ khuôn (không méo)
        for f in m.values():
            (W, H), (x0, y0, x1, y1) = Image.open(TPL_DIR / f).size, _tpl_box(TPL_DIR / f)
            assert abs((x1 - x0) / (y1 - y0) - W / H) < .01 and (x0 == 0 or y0 == 0), f
    assert export_piece("ao_sau_phang", pc, td2 / "h.png", td2, "hoodie") == "khuôn hoodie_back"
    assert Image.open(td2 / "h.png").size == Image.open(TPL_DIR / "hoodie_back.jpg").size
    # jumpsuit: ao+quan -> 1 mảnh bo_* (box gộp); mũ chỉ khi vision trả "hood"
    tbh = {"type": "top_bottom", "top_front": "base red; a", "top_back": "base red", "pants_front": "base red; b",
           "pants_back": "base red", "sleeves_same": True, "sl_front": "base red", "sl_back": "base red",
           "hood": "base red; ears", "boxes": {"top_front": [100, 100, 500, 400], "pants_front": [120, 400, 480, 900]}}
    js_plan = {p["slug"]: p for p in _pieces_plan(tbh, "jumpsuit")}
    assert set(js_plan) == {"bo_truoc", "bo_sau", "tay_truoc", "tay_sau", "mu"}
    assert js_plan["bo_truoc"]["box"] == [100, 100, 500, 900] and "LEGS: base red; b" in js_plan["bo_truoc"]["desc"]
    assert {p["slug"] for p in _pieces_plan({k: v for k, v in tbh.items() if k != "hood"})} == \
        {"ao_truoc", "ao_sau", "quan_truoc", "quan_sau", "tay_truoc", "tay_sau"}
    assert _filter_by_garment({"bo_truoc", "mu", "quan_sau"}, "bottom") == {"quan_sau"}
    assert {p["slug"] for p in _pieces_plan(tbh, "jumpsuit", "top")} == {"ao_truoc", "ao_sau", "tay_truoc", "tay_sau", "mu"}
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
    ap.add_argument("--kind", choices=sorted(CLOTH_TPL), help="loại có khuôn mảnh thật (batch suy từ SKU)")
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args()
    if a.selfcheck:
        _selfcheck()
    elif a.front:
        if a.emit:
            globals()["EMIT"] = a.emit
        globals()["COLOR"], globals()["HINT"], globals()["GARMENT"] = a.color, a.hint, a.garment
        globals()["KIND"] = a.kind
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
