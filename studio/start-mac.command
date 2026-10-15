#!/bin/bash
# ============================================================
#  Flat Studio — CHAY tren macOS
#  Double-click de chay, hoac: ./start-mac.command [port]
#  Nut Restart trong app se thoat server -> vong lap duoi bat lai (nap code moi).
#  Dong cua so Terminal de tat han.
# ============================================================
cd "$(dirname "$0")" || exit 1
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8      # tranh crash khi in duong dan tieng Viet

PORT="${1:-8770}"
PY=./.venv/bin/python
[ -x "$PY" ] || PY=python3                       # chua chay setup-mac thi thu python3 he thong

# Python tu python.org (Mac Intel) khong doc keychain -> urllib loi CERTIFICATE_VERIFY_FAILED.
# Tro SSL sang CA bundle cua certifi (ap dung ca tien trinh con: chatgpt-imagegen, openart...).
CA="$("$PY" -c 'import certifi; print(certifi.where())' 2>/dev/null)"
[ -n "$CA" ] && export SSL_CERT_FILE="$CA" REQUESTS_CA_BUNDLE="$CA"

# Ban cai tu zip khong co .git -> tu noi vao repo 1 lan (checkout -f chi ghi de file
# trong repo; .venv, .env, bin/ la untracked/ignored nen giu nguyen).
REPO_URL="https://github.com/MinhThuat/process-image-ds.git"
if git --version >/dev/null 2>&1 && ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "[start] chua phai git repo -> noi vao $REPO_URL ..."
  git init -q && git remote add origin "$REPO_URL" && git fetch -q origin main \
    && git checkout -q -f -B main origin/main
  git rev-parse --verify -q HEAD >/dev/null && echo "[start] OK — da bat autoupdate." \
    || { echo "[start] !! noi repo that bai (mang?) — lan sau chay lai."; rm -rf .git; }
fi

# Tu cap nhat code tu git remote moi 60s (bash native tren Mac) — chi khi la git repo.
if [ -f autoupdate.sh ] && git rev-parse --git-dir >/dev/null 2>&1; then
  bash autoupdate.sh "$(pwd)" >/dev/null 2>&1 &
fi

# Mo trinh duyet sau 2s (cho server len).
( sleep 2; open "http://127.0.0.1:$PORT" ) >/dev/null 2>&1 &

# Vong lap khoi dong lai.
while true; do
  "$PY" server.py --port "$PORT"
  echo
  echo "[start] server dung — khoi dong lai sau 2s (dong cua so de tat han)..."
  sleep 2
done
