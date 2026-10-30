#!/usr/bin/env bash
# All-day unattended options-mimic-bot run on Alpaca PAPER (never live money).
# Runs `python run.py --paper` (active profile) until 16:05 ET (just after the 16:00 ET close),
# restarts it if it crashes, and appends everything to logs/run_YYYY-MM-DD.log.
#   scripts/run_paper_day.sh                 # default stop time 16:05 America/New_York
#   STOP_AT=15:30 scripts/run_paper_day.sh   # custom stop time (ET)
#   EXTRA_ARGS="--symbols SPY,QQQ" scripts/run_paper_day.sh
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
mkdir -p logs
LOG="logs/run_$(TZ=America/New_York date +%F).log"
say() { echo "[$(TZ=America/New_York date '+%F %H:%M:%S %Z')] $*" | tee -a "$LOG"; }

# --- Python env: prefer .venv, create it if missing (system pip is externally managed) ---
PY=""
if [ -x .venv/bin/python ]; then PY=.venv/bin/python
elif python3 -m venv .venv >>"$LOG" 2>&1; then
  PY=.venv/bin/python
  say "created .venv"
  .venv/bin/python -m pip install -q --upgrade pip >>"$LOG" 2>&1 || true
  .venv/bin/python -m pip install -q -r requirements.txt >>"$LOG" 2>&1 \
    || say "warning: pip install -r requirements.txt failed (only needed for charts; the bot is stdlib-only)"
else
  PY=$(command -v python3)
  say "warning: could not create .venv, using $PY"
fi
"$PY" -c 'import sys; assert sys.version_info >= (3, 11)' || { say "need Python 3.11+ ($PY)"; exit 1; }

# --- keys and paper URL. .env is loaded in Python. A refused URL is not printed (it may not be paper,
#     and it may have credentials in the userinfo). The shell glob used to allow paper-api.alpaca.markets.evil. ---
"$PY" -c 'import os,sys
from mimic_bot.env import load_dotenv, assert_paper_base
load_dotenv()
try:
    assert_paper_base()
except RuntimeError:
    sys.exit(2)
sys.exit(0 if os.environ.get("APCA_API_KEY_ID") and os.environ.get("APCA_API_SECRET_KEY") else 1)'
rc=$?
if [ "$rc" -eq 2 ]; then say "refusing APCA_API_BASE_URL: paper only (https://paper-api.alpaca.markets)"; exit 1; fi
if [ "$rc" -ne 0 ]; then say "missing APCA_API_KEY_ID / APCA_API_SECRET_KEY (paper keys). Export them or put them in .env"; exit 1; fi

# --- one instance at a time: a second launch (routine retry, manual run) exits instead of double-trading ---
LOCK="logs/.paper_day.lock"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK"
  if ! flock -n 9; then
    say "another options-mimic-bot paper run is already active ($LOCK); exiting"; exit 0
  fi
else
  # flock is missing on some images; mkdir is atomic. A dead pid loses the lock so a crash can't stick it.
  LOCKDIR="$LOCK.d"
  if ! mkdir "$LOCKDIR" 2>/dev/null; then
    old=$(cat "$LOCKDIR/pid" 2>/dev/null || true)
    if [ -n "${old}" ] && kill -0 "$old" 2>/dev/null; then
      say "another options-mimic-bot paper run is already active (pid $old); exiting"; exit 0
    fi
    rm -rf "$LOCKDIR"
    mkdir "$LOCKDIR" || { say "could not take $LOCKDIR"; exit 1; }
  fi
  echo $$ > "$LOCKDIR/pid"
  trap 'rm -rf "$LOCKDIR"' EXIT
  say "flock not installed; using $LOCKDIR"
fi

# --- read-only preflight (account, clock, options level). Retries; a failure here is logged, not fatal ---
for i in 1 2 3; do
  if "$PY" run.py --paper --check >>"$LOG" 2>&1; then say "preflight ok (read-only account check)"; break; fi
  say "preflight check failed (attempt $i/3)"; sleep 20
done

STOP_AT=${STOP_AT:-16:05}
END=$(TZ=America/New_York date -d "today $STOP_AT" +%s)
say "paper day starting with $PY; stops at $STOP_AT ET"
n=0
wait_s=15
while :; do
  now=$(date +%s); left=$((END - now))
  [ "$left" -le 0 ] && break
  n=$((n + 1)); say "start #$n (${left}s left)"
  t0=$(date +%s)
  timeout --signal=INT --kill-after=30 "$left" "$PY" -u run.py --paper ${EXTRA_ARGS:-} >>"$LOG" 2>&1
  rc=$?
  now=$(date +%s)
  # timeout sends SIGINT; run.py catches it and exits 0, so 124 is not the only "we're done" code.
  [ "$rc" -eq 124 ] || [ "$now" -ge "$END" ] && break
  # crash-loop backoff: 15s, then 30, 60, 120 while it keeps dying within 2 min. A longer run resets to 15s.
  # On restart the bot reconciles with the paper account, so it never double-enters or loses track of a position.
  ran=$(( now - t0 ))
  if [ "$ran" -lt 120 ]; then
    say "run.py exited with code $rc after ${ran}s; restarting in ${wait_s}s"
    sleep "$wait_s"
    wait_s=$(( wait_s * 2 )); [ "$wait_s" -gt 120 ] && wait_s=120
  else
    wait_s=15
    say "run.py exited with code $rc; restarting in ${wait_s}s"
    sleep "$wait_s"
  fi
done
say "done for the day. Event log: logs/paper_$(TZ=America/New_York date +%F).jsonl"
