#!/bin/bash
# Watch pending decisions for an ARCH project
# Usage: ./watch-decisions.sh [state-dir]
# Default: ./state
# Press Ctrl+C to stop

STATE_DIR="${1:-./state}"
FILE="$STATE_DIR/pending_decisions.json"

if [ ! -f "$FILE" ]; then
  echo "Not found: $FILE"
  echo "Usage: $0 [path/to/state]"
  exit 1
fi

while true; do
  clear
  echo "=== Pending Decisions ($(date +%H:%M:%S)) ==="
  echo ""
  python3 -c "
import json
with open('$FILE') as f:
    data = json.load(f)
pending = [d for d in data if d.get('answer') is None]
print(f'{len(pending)} pending / {len(data)} total')
print()
for d in pending:
    print(d['question'][:200])
    if d.get('options'):
        for i,o in enumerate(d['options']): print(f'  [{i+1}] {o}')
    print()
if not pending:
    print('No pending decisions.')
"
  sleep 1
done
