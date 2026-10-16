#!/usr/bin/env bash
# Cron: kiểm tra mẫu GEN mới trên Base và chạy batch.py (gen + up). Lịch (crontab, tag # flat-gen):
#   8:30 9:30 10:30 11:30 · 13:00 14:00 15:00 16:00 17:00 · và ngay khi mở máy (@reboot).
# Chỉ chạy trong khung 08:30-12:00 và 13:00-17:30 (mở máy ngoài giờ -> ghi log rồi bỏ qua).
# batch.py tự khoá (batch.lock): lượt trước chưa xong thì lượt này tự thoát, không chạy chồng.
# Log theo dõi: flat_studio_data/cron_gen.log (mỗi lượt 1 khối, kèm output batch).
cd "$(dirname "$0")" || exit 1
export PATH="$HOME/.nvm/versions/node/v22.23.2/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
LOG=../flat_studio_data/cron_gen.log
now=$(date +%H%M)
{
  echo "=== $(date '+%F %T') [${1:-cron}] ==="
  if { [ "$now" -ge 0830 ] && [ "$now" -lt 1200 ]; } || { [ "$now" -ge 1300 ] && [ "$now" -lt 1730 ]; }; then
    python3 batch.py; rc=$?
    echo "--- xong $(date '+%T') (exit $rc)"
  else
    echo "--- ngoài giờ chạy (08:30-12:00, 13:00-17:30) -> bỏ qua"
  fi
} >> "$LOG" 2>&1
exit "${rc:-0}"
