import os
import sqlite3
from datetime import datetime
from pathlib import Path

root=Path('/var/lib/stock-journal/backups')
root.mkdir(parents=True,exist_ok=True)
target=root/('journal-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.db')
with sqlite3.connect('/var/lib/stock-journal/journal.db') as src,sqlite3.connect(target) as dst:
    src.backup(dst)
target.chmod(0o600)
for old in sorted(root.glob('journal-*.db'))[:-30]:
    if old.resolve().parent==root.resolve():
        old.unlink()
print('SQLite backup completed.')
