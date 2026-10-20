#!/usr/bin/env bash
# Cron: kiểm tra mẫu GEN chưa làm trên Base và chạy batch.py tới hết (gen + up).
# Lịch (crontab, tag # flat-gen): chỉ 21:00 mỗi ngày. Ban ngày KHÔNG cron — USER nhờ Claude chạy tay
# (`./cron_gen.sh manual`) khi cần.
# batch.py tự khoá (batch.lock): lượt trước chưa xong thì lượt này tự thoát, không chạy chồng.
# Log theo dõi: flat_studio_data/cron_gen.log (mỗi lượt 1 khối, kèm output batch).
cd "$(dirname "$0")" || exit 1
# node bản mới nhất của nvm (claude, lark-cli, codex nằm ở đó) — không ghi cứng số phiên bản
NODE_BIN=$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -1)
export PATH="$NODE_BIN:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
LOG=../flat_studio_data/cron_gen.log
# log > 5MB -> đổi sang cron_gen.log.1 (giữ 1 bản cũ)
[ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 5000000 ] && mv -f "$LOG" "$LOG.1"
{
  echo "=== $(date '+%F %T') [${1:-cron}] ==="
  python3 batch.py; rc=$?
  # cron tối: xong hết mẫu -> soát lại 1 lượt, gen lại riêng các mảnh QA FAIL quá tệ (batch.py BAD_SCORE)
  [ "${1:-cron}" = cron ] && [ "$rc" = 0 ] && { python3 batch.py --fix-bad; rc=$?; }
  echo "--- xong $(date '+%T') (exit $rc)"
} >> "$LOG" 2>&1
exit "${rc:-0}"
