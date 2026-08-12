#!/bin/zsh
# Consistent snapshot of the live fly DB -> timestamped local file.
# Backup runs on-box via the sqlite backup API (safe against the live WAL),
# then downloads. Usage: zsh scripts/pull-fly-db.sh [app-name]
set -e
app=${1:-bacchus-mm}
out="fly-snapshot-$(date -u +%Y%m%dT%H%M%SZ).db"
fly ssh console -a "$app" -C 'python3 -c "import sqlite3; s=sqlite3.connect(\"file:/app/data/bacchus.db?mode=ro\",uri=True); d=sqlite3.connect(\"/tmp/snap.db\"); s.backup(d); d.close(); print(\"backup ok\")"'
fly ssh sftp get /tmp/snap.db "$out" -a "$app"
echo "$out"
