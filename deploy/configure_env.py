"""Run with sudo on the existing server. Secret values never leave this process."""
import os
import shlex
from pathlib import Path

source=Path('/opt/bots/kiwoom-stock/.env')
values={}
for line in source.read_text().splitlines():
    line=line.strip()
    if not line or line.startswith('#') or '=' not in line:
        continue
    key,value=line.split('=',1)
    if key.strip() in ('KIWOOM_APP_KEY','KIWOOM_SECRET_KEY'):
        parsed=shlex.split(value,comments=True)
        values[key.strip()]=parsed[0] if parsed else ''
if not all(values.get(k) for k in ('KIWOOM_APP_KEY','KIWOOM_SECRET_KEY')):
    raise SystemExit('Broker quote keys were not found. No existing bot changes were made.')
values.update(JOURNAL_DB='/var/lib/stock-journal/journal.db',QUOTE_ENABLED='1',
              ALLOWED_ORIGINS='https://mathtjungsw.github.io,https://stock-journal.52-79-211-161.sslip.io',
              MARKET_OVERRIDES='/opt/stock-journal/deploy/market-overrides.json')
target=Path('/etc/stock-journal.env')
if target.exists():
    raise SystemExit('Environment already exists; inspect before changing.')
target.write_text(''.join(f'{k}="{v}"\n' for k,v in values.items()))
target.chmod(0o600)
print('Isolated quote environment created; only two broker quote credentials copied.')
