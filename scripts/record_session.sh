#!/usr/bin/env bash
# Start the chain recorder if, and only if, this is a session it should record.
#
# launchd fires this every 30 minutes rather than once at the open. This machine
# runs on Israel time and the local-to-Eastern offset moves between 6 and 8
# hours across two countries' DST changes, so a fixed local trigger drifts off
# the open twice a year; gating on Eastern time is exact all year. Firing often
# also doubles as a crash restart — a recorder that dies at 11:00 ET is back
# within half an hour, and the roadmap asks for five consecutive days with no
# gaps.
#
# IBKR_PORT comes from the launchd agent, never from this file: the repo is
# public and must not carry a default that points at a live account.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

et_dow=$(TZ=America/New_York date +%u)
et_hhmm=$(TZ=America/New_York date +%H%M)

# Weekdays only. Exchange holidays are deliberately not listed: on a holiday the
# chain resolves empty and the recorder exits saying so, which is the same
# outcome with none of the calendar to maintain.
[ "$et_dow" -le 5 ] || exit 0

# From 09:00 ET, so 484 subscriptions are established before the open, until
# 16:00 ET. The recorder stops itself at the 16:15 ET SPXW close.
[ "$((10#$et_hhmm))" -ge 900 ] && [ "$((10#$et_hhmm))" -lt 1600 ] || exit 0

pgrep -f "spx0dte.ingest.recorder" >/dev/null && exit 0

mkdir -p logs
log="logs/recorder-$(TZ=America/New_York date +%Y-%m-%d).log"
echo "--- $(date -u +%FT%TZ) starting recorder (${et_hhmm} ET, port ${IBKR_PORT:-unset})" >>"$log"

# caffeinate: a Mac that falls asleep stops recording silently.
exec caffeinate -is .venv/bin/python -m spx0dte.ingest.recorder >>"$log" 2>&1
