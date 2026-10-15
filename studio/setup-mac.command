#!/bin/bash
# ============================================================
#  Flat Studio — SETUP cho macOS (chay 1 lan)
#  Apple Silicon: cai Python/Node/Git qua Homebrew.
#  Intel (x86_64): Homebrew moi da BO ho tro -> dung bo cai chinh thuc
#    (Python tu python.org, Node tu nodejs.org, Git qua Xcode CLT).
#  Sau do: .venv + thu vien Python + codex (npm) + OpenArt CLI ban Mac.
#  Double-click de chay, hoac: ./setup-mac.command
# ============================================================
cd "$(dirname "$0")" || exit 1
echo "== Flat Studio — SETUP macOS =="
ARCH="$(uname -m)"; echo "  Kien truc may: $ARCH"
have(){ command -v "$1" >/dev/null 2>&1; }

# ---- 1. Python 3.12 + Node + Git ----
if have brew; then
  echo "== 1. Python / Node / Git (qua Homebrew) =="
  for pkg in python@3.12 node git; do
    if brew list "$pkg" >/dev/null 2>&1; then echo "  da co $pkg"; else echo "  cai $pkg..."; brew install "$pkg"; fi
  done
else
  echo "== 1. Khong co Homebrew (may Intel) -> dung bo cai chinh thuc =="
  # Git — di kem Xcode Command Line Tools
  if have git; then echo "  da co git"; else
    echo "  Cai Git: hop thoai Xcode Command Line Tools se hien ra -> bam Install."
    echo "  Cai xong (~5-10 phut) HAY CHAY LAI file nay."
    xcode-select --install 2>/dev/null
  fi
  # Python 3.12 — installer universal2 (chay ca Intel + ARM), can macOS 11+
  if have python3 && python3 -c 'import sys; exit(0 if sys.version_info[:2]>=(3,9) else 1)' 2>/dev/null; then
    echo "  da co python3 ($(python3 --version 2>&1))"
  else
    echo "  Tai + cai Python 3.12 (se hoi MAT KHAU may)..."
    if curl -fL -o /tmp/dsds_py.pkg https://www.python.org/ftp/python/3.12.8/python-3.12.8-macos11.pkg; then
      sudo installer -pkg /tmp/dsds_py.pkg -target / || echo "  !! Cai Python that bai"
    else echo "  !! Tai Python that bai (kiem tra mang)"; fi
  fi
  # Node 20 — cho codex (npm), installer chinh thuc
  if have node; then echo "  da co node ($(node --version 2>&1))"; else
    echo "  Tai + cai Node 20 (se hoi MAT KHAU may)..."
    if curl -fL -o /tmp/dsds_node.pkg https://nodejs.org/dist/v20.18.1/node-v20.18.1.pkg; then
      sudo installer -pkg /tmp/dsds_node.pkg -target / || echo "  !! Cai Node that bai"
    else echo "  !! Tai Node that bai (kiem tra mang)"; fi
  fi
fi

hash -r 2>/dev/null   # quen cache PATH cu de thay binary vua cai

# ---- 2. Xac dinh python de tao venv ----
PY="$(command -v python3 || true)"
[ -z "$PY" ] && [ -x /usr/local/bin/python3 ] && PY=/usr/local/bin/python3
[ -z "$PY" ] && { echo "!! Khong tim thay python3. Cai Python xong roi chay lai file nay."; read -r -p "Enter de thoat..."; exit 1; }

# ---- 3. venv + thu vien Python ----
# Dung venv de tranh loi "externally-managed-environment" (PEP 668).
echo "== 2. Moi truong Python (.venv) + thu vien =="
"$PY" -m venv .venv || { echo "!! Tao .venv that bai"; read -r -p "Enter..."; exit 1; }
./.venv/bin/pip install --upgrade pip >/dev/null
./.venv/bin/pip install "aiohttp>=3.9" pillow numpy opencv-python-headless scipy "openai>=1.0" python-dotenv certifi \
  || { echo "!! pip install that bai"; read -r -p "Enter..."; exit 1; }

# ---- 4. codex (npm global) ----
# Brew (Apple Silicon): prefix thuoc quyen user, khong can sudo.
# Node tu nodejs.org (Intel): global prefix o /usr/local thuoc root -> can sudo.
echo "== 3. codex (gen chinh) =="
if have codex; then echo "  da co codex"
elif have npm; then
  if have brew; then npm install -g @openai/codex || echo "  !! cai codex that bai"
  else echo "  (may Intel) cai codex bang sudo — co the hoi mat khau..."; sudo npm install -g @openai/codex || echo "  !! cai codex that bai"; fi
  hash -r 2>/dev/null
else echo "  !! chua co npm/Node -> bo qua codex (cai Node roi chay lai)"; fi

# ---- 5. .env ----
echo "== 4. .env (ARK_API_KEY cho dola-seed) =="
if [ -f .env ] && grep -q '^ARK_API_KEY=' .env; then echo "  OK: co ARK_API_KEY"
else echo "  !! Tao file .env canh day, them dong:  ARK_API_KEY=ark-..."; fi

# ---- 6. OpenArt CLI ban Mac (fallback) ----
# Ban Mac (arm64 + amd64) da BUNDLE san trong bin/openart-darwin-*. Chi copy dung kien truc.
echo "== 5. OpenArt CLI (fallback) — ban Mac =="
mkdir -p bin
[ "$ARCH" = "x86_64" ] && OA=amd64 || OA=arm64          # Intel=amd64, Apple Silicon=arm64
if [ -x "bin/openart-darwin-$OA" ]; then
  cp "bin/openart-darwin-$OA" bin/openart; chmod +x bin/openart
  echo "  OK: bin/openart (Mac $OA — dung ban bundle san)"
else
  url="https://github.com/OpenArt-AI/cli/releases/download/v0.1.1/openart_0.1.1_darwin_${OA}.tar.gz"
  echo "  bundle thieu, thu tai: $url"
  if curl -fsSL "$url" -o /tmp/dsds_oa.tar.gz && ( cd /tmp && rm -f openart && tar -xzf dsds_oa.tar.gz openart ); then
    mv /tmp/openart bin/openart; chmod +x bin/openart; echo "  OK: bin/openart (Mac $OA — tai ve)"
  else echo "  !! Khong co OpenArt Mac. Bo qua fallback — gen chinh qua codex van chay."; fi
fi
if [ -x bin/openart ]; then echo "  Mo dang nhap OpenArt trong trinh duyet, xong quay lai..."; ./bin/openart login || true; fi

# ---- 7. Dang nhap codex ----
echo "== 6. Dang nhap ChatGPT/codex =="
if [ -f "$HOME/.codex/auth.json" ]; then echo "  OK: da dang nhap codex"
elif have codex; then codex login || true; fi

echo
echo "XONG. Chay tool:  double-click start-mac.command  (hoac ./start-mac.command)"
read -r -p "Nhan Enter de dong..."
