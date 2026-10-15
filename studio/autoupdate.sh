#!/usr/bin/env bash
# Tu cap nhat code tu git remote moi 60s (start.bat chay nen file nay).
# User chi la nguoi dung, khong sua code -> hard reset ve remote, khong ket conflict.
# Du lieu (anh) nam o ~/dsds_studio/ (ngoai repo) nen reset an toan.
#
# QUAN TRONG: start.bat copy file nay ra %TEMP% roi chay, truyen thu muc repo vao $1.
# Nho vay `git reset --hard` co the GHI DE autoupdate.sh trong repo ma khong bi
# "Permission denied" (Windows khoa file dang chay).
#   bash autoupdate.sh <repo_dir>          # vong lap moi 60s
#   bash autoupdate.sh <repo_dir> --once   # kiem tra 1 lan roi thoat
set -u

ONCE=""; REPO=""
for a in "$@"; do
  case "$a" in
    --once) ONCE=1 ;;
    *) REPO="$a" ;;
  esac
done
cd "${REPO:-$(dirname "$0")}" 2>/dev/null || { echo "[autoupdate] khong vao duoc repo: ${REPO}"; exit 0; }

BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)" || {
  echo "[autoupdate] chua phai git repo (chay: git init && git remote add origin <url>) — bo qua."; exit 0; }

update_once() {
  git remote get-url origin >/dev/null 2>&1 || return 0     # chua co remote -> bo qua
  git fetch -q origin "$BRANCH" 2>/dev/null || return 0     # mat mang -> thu lai sau
  local l r; l="$(git rev-parse HEAD)"; r="$(git rev-parse "origin/$BRANCH" 2>/dev/null)" || return 0
  [ "$l" = "$r" ] && return 0
  git reset --hard -q "origin/$BRANCH" \
    && echo "[autoupdate] da cap nhat code (${r:0:7}) — bam Restart trong app de nap lai."
}

[ -n "$ONCE" ] && { update_once; exit 0; }
while true; do update_once; sleep 60; done
