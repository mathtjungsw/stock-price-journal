"""Local/server operator commands; not exposed by the web API."""
import argparse
import json
import secrets
import time
from pathlib import Path
from .auth import digest
from .journal import Journal

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--db',required=True)
    parser.add_argument('command',choices=['import','setup','backup'])
    parser.add_argument('--file')
    args=parser.parse_args()
    journal=Journal(args.db)
    if args.command=='import':
        with open(args.file,encoding='utf-8') as f:
            journal.import_sheet(json.load(f))
        print(json.dumps({'imported':True,'counts':{a:len(journal.rows(a)) for a in ('joint','soyeon')}},ensure_ascii=False))
    elif args.command=='setup':
        if journal.meta('password_hash'):
            raise SystemExit('Password already set; do not reset automatically.')
        token=secrets.token_urlsafe(32)
        journal.set_meta('setup_hash',digest(token))
        journal.set_meta('setup_expires',int(time.time()+86400))
        # Store onboarding token in a private handoff file, not process args or public logs.
        path=Path(args.file)
        path.write_text(json.dumps({'setup_token':token}),encoding='utf-8')
        path.chmod(0o600)
        print('One-time setup token saved to private file; expires in 24 hours.')
    else:
        journal.backup(args.file)
        print('Backup completed.')

if __name__=='__main__':
    main()
