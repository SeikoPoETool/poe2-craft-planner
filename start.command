#!/bin/bash
# Double-click to start the app: runs the local server and opens it in the browser.
# Keep this window open while you use the app; close it to stop.
cd "$(dirname "$0")"
# a package downloaded from the internet is quarantined by macOS: the bundled LuaJIT would not start
xattr -dr com.apple.quarantine . 2>/dev/null
if ! command -v python3 >/dev/null || ! python3 -c "" 2>/dev/null; then
  echo "Python 3 is needed: macOS will offer to install it (Command Line Tools). Then double-click start.command again."
  xcode-select --install 2>/dev/null
  read -r -p "Press Enter to close"
  exit 1
fi
if [ ! -x engine/luajit ]; then
  echo "First start: setting up (needs a minute)..."
  ./install.sh || { read -r -p "Setup failed. Press Enter to close"; exit 1; }
fi
OPEN_BROWSER=1 exec python3 server.py
