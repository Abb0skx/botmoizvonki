"""Host-side daily consistent SQLite backup. Keeps 14 daily snapshots."""
from datetime import datetime, timezone
from pathlib import Path
import os
import sqlite3

root = Path('/opt/texnikach-instagram-inbox')
dest = root / 'backups'
dest.mkdir(mode=0o700, exist_ok=True)
source = root / 'data/inbox.db'
if not source.exists():
    raise SystemExit('Inbox database not initialized')
target = dest / ('inbox-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '.db')
with sqlite3.connect(f'file:{source}?mode=ro', uri=True) as src, sqlite3.connect(target) as dst:
    src.backup(dst)
os.chmod(target, 0o600)
for old in sorted(dest.glob('inbox-????????-??????.db'))[:-14]:
    old.unlink()
print(target.name)
