#!/usr/bin/env bash
# Install the launchd agent that records every trading session.
#
# The IBKR port is an argument and is written only into the machine-local
# plist, so nothing committed to this public repo names a live account.
#
#   ./scripts/install_launchd.sh 4001   # live IB Gateway, read-only
#   ./scripts/install_launchd.sh 7497   # TWS paper
#
# Remove it with:
#   launchctl bootout "gui/$UID/com.spx0dte.recorder"
#   rm ~/Library/LaunchAgents/com.spx0dte.recorder.plist
set -euo pipefail

port="${1:-}"
if [ -z "$port" ]; then
    echo "usage: $0 <IBKR_PORT>   (4001 IB Gateway, 7496 TWS live, 7497 TWS paper)" >&2
    exit 2
fi

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
label="com.spx0dte.recorder"
plist="$HOME/Library/LaunchAgents/$label.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$repo/logs"

cat >"$plist" <<XML
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$label</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$repo/scripts/record_session.sh</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>IBKR_PORT</key>
        <string>$port</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>$repo</string>
    <key>StartInterval</key>
    <integer>1800</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$repo/logs/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$repo/logs/launchd.err.log</string>
</dict>
</plist>
XML

launchctl bootout "gui/$UID/$label" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$plist"

echo "installed $plist"
echo "  port      $port"
echo "  fires     every 30 min; records 09:00-16:00 ET on weekdays"
echo "  logs      $repo/logs/recorder-YYYY-MM-DD.log"
launchctl print "gui/$UID/$label" | grep -E "state|program|last exit" | head -5
