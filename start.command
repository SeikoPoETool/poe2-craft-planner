#!/bin/bash
# Double-click to start the app: runs the local server and opens it in the browser.
cd "$(dirname "$0")"
if curl -s -o /dev/null http://127.0.0.1:8765/; then
  open http://127.0.0.1:8765/
  exit 0
fi
python3 server.py &
for i in $(seq 1 40); do
  curl -s -o /dev/null http://127.0.0.1:8765/ && break
  sleep 0.25
done
open http://127.0.0.1:8765/
wait
