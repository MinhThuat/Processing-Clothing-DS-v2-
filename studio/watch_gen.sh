#!/usr/bin/env bash
# Mỗi giờ chạy batch.py 1 lần: có mẫu GEN mới (view GEN, chưa có ảnh) thì gen + up; không có thì thoát
# êm, không báo Lark. batch.py tự khoá (batch.lock) nên lượt trước chạy lâu thì lượt sau tự bỏ, không chồng.
# Chạy:  nohup ./watch_gen.sh >/dev/null 2>&1 &      Dừng:  pkill -f watch_gen.sh
# Đổi chu kỳ: WATCH_INTERVAL=1800 ./watch_gen.sh
cd "$(dirname "$0")"
LOG=../flat_studio_data/watch_gen.log
while true; do
  echo "=== $(date '+%F %T') chạy batch ===" >> "$LOG"
  python3 batch.py >> "$LOG" 2>&1
  sleep "${WATCH_INTERVAL:-3600}"
done
