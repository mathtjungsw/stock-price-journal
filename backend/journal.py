"""Personal journal only. No broker order or account APIs are used here."""
import json
import sqlite3
from contextlib import contextmanager
from calendar import monthrange
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from .market import now_kst

STOCKS = [
    {"code": "005930", "name": "삼성전자"},
    {"code": "009150", "name": "삼성전기"},
    {"code": "035420", "name": "NAVER"},
    {"code": "402340", "name": "SK스퀘어"},
    {"code": "018260", "name": "삼성에스디에스"},
    {"code": "000660", "name": "SK하이닉스"},
]
NAMES = {s['code']: s['name'] for s in STOCKS}

class JournalError(ValueError):
    pass

def one_month_later(day):
    year=day.year+(day.month==12)
    month=day.month%12+1
    return date(year,month,min(day.day,monthrange(year,month)[1]))

def sell_reference_price(cost, quantity):
    """Minimum ordinary-share quote tick reaching 5% before selling costs.

    KRX ordinary shares: https://regulation.krx.co.kr/contents/RGL/03/03010100/RGL03010100T3.jsp
    The six supported stocks are ordinary shares, not ETF/ETN products.
    """
    target=Decimal(cost)*Decimal('1.05')/Decimal(quantity)
    tick=1000
    for ceiling,unit in ((2000,1),(5000,5),(20000,10),(50000,50),(200000,100),(500000,500)):
        if target<ceiling:
            tick=unit
            break
    return int((target/Decimal(tick)).to_integral_value(rounding=ROUND_CEILING))*tick

class Journal:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, opening_cash INTEGER NOT NULL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
                date TEXT NOT NULL, code TEXT, kind TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 0, price INTEGER NOT NULL DEFAULT 0,
                fee INTEGER NOT NULL DEFAULT 0, amount INTEGER NOT NULL DEFAULT 0,
                memo TEXT NOT NULL DEFAULT '', deleted INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS sessions (hash TEXT PRIMARY KEY, expires INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS quotes (code TEXT, market TEXT, payload TEXT NOT NULL,
                PRIMARY KEY(code,market));
              CREATE INDEX IF NOT EXISTS trades_account_date ON trades(account_id,date);
            ''')
            db.executemany('INSERT OR IGNORE INTO accounts(id,name) VALUES (?,?)',
                           [('joint','공동계좌'), ('soyeon','소연계좌')])

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def meta(self, key, default=None):
        with self.db() as db:
            row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row['value'] if row else default

    def set_meta(self, key, value):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key,str(value)))

    def rows(self, account_id, deleted=False):
        with self.db() as db:
            return [dict(r) for r in db.execute(
                'SELECT * FROM trades WHERE account_id=? AND deleted=? ORDER BY date,created_at,id',
                (account_id, int(deleted)))]

    def account(self, account_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
        if not row:
            raise JournalError('계좌를 찾을 수 없습니다.')
        return dict(row)

    @staticmethod
    def calculate(rows, opening_cash=0, today=None):
        today=today or now_kst().date()
        positions, realized, cash = {}, Decimal(0), Decimal(opening_cash)
        for r in rows:
            if r.get('deleted'):
                continue
            q, p, fee = Decimal(r['quantity']), Decimal(r['price']), Decimal(r['fee'])
            if r['kind'] in ('deposit','withdraw'):
                cash += Decimal(r['amount']) * (1 if r['kind']=='deposit' else -1)
                continue
            pos = positions.setdefault(r['code'], {'quantity':Decimal(0),'cost':Decimal(0),'last_buy_date':None})
            if r['kind']=='buy':
                pos['quantity'] += q
                pos['cost'] += q*p + fee
                cash -= q*p + fee
                pos['last_buy_date']=max(pos['last_buy_date'] or r['date'],r['date'])
            elif r['kind']=='sell':
                if q > pos['quantity']:
                    raise JournalError(f"{r['date']} {NAMES.get(r['code'],r['code'])}: 매도 수량이 당시 보유 수량을 초과합니다.")
                cost = pos['cost'] / pos['quantity'] * q
                pos['cost'] -= cost
                pos['quantity'] -= q
                realized += q*p-fee-cost
                cash += q*p-fee
        holdings=[]
        for code,pos in positions.items():
            if pos['quantity']>0:
                buy_date=date.fromisoformat(pos['last_buy_date'])
                next_buy=one_month_later(buy_date)
                sell_price=sell_reference_price(pos['cost'],pos['quantity'])
                holdings.append({'code':code,'name':NAMES[code], 'quantity':int(pos['quantity']),
                    'cost':float(pos['cost']), 'average':float(pos['cost']/pos['quantity']),
                    'target':float(pos['cost']/pos['quantity']*Decimal('1.05')),
                    'sell_reference_price':sell_price,'sell_reference_amount':sell_price*int(pos['quantity']),
                    'last_buy_date':buy_date.isoformat(),'next_buy_date':next_buy.isoformat(),
                    'buy_available':today>=next_buy})
        return {'holdings':holdings, 'cash':float(cash), 'realized':float(realized),
                'cost':sum(h['cost'] for h in holdings)}

    def portfolio(self, account_id):
        account = self.account(account_id)
        return {**account, **self.calculate(self.rows(account_id), account['opening_cash'])}

    def save(self, record, record_id=None, revision=None):
        now = datetime.now(timezone.utc).isoformat()
        self.account(record['account_id'])
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if record_id:
                old=db.execute('SELECT * FROM trades WHERE id=?', (record_id,)).fetchone()
                if not old or old['deleted'] or old['account_id']!=record['account_id']:
                    raise JournalError('수정할 기록을 찾을 수 없습니다.')
                if old['revision'] != revision:
                    raise JournalError('다른 기기에서 변경된 기록입니다. 새로고침 후 다시 수정하세요.')
                db.execute('''UPDATE trades SET date=?,code=?,kind=?,quantity=?,price=?,fee=?,amount=?,memo=?,
                              revision=revision+1,updated_at=? WHERE id=?''',
                    tuple(record[k] for k in ['date','code','kind','quantity','price','fee','amount','memo'])+(now,record_id))
            else:
                record_id=record['id']
                old=db.execute('SELECT * FROM trades WHERE id=?', (record_id,)).fetchone()
                if old:
                    expected={k:old[k] for k in record}
                    if expected!=record:
                        raise JournalError('이미 사용한 저장 번호입니다. 다시 입력해 주세요.')
                    return dict(old)
                db.execute('''INSERT INTO trades(id,account_id,date,code,kind,quantity,price,fee,amount,memo,created_at,updated_at)
                              VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                    tuple(record[k] for k in ['id','account_id','date','code','kind','quantity','price','fee','amount','memo'])+(now,now))
            rows=[dict(r) for r in db.execute('SELECT * FROM trades WHERE account_id=? AND deleted=0 ORDER BY date,created_at,id', (record['account_id'],))]
            self.calculate(rows)  # Validate complete date order atomically, including edits to past buys.
            return dict(db.execute('SELECT * FROM trades WHERE id=?',(record_id,)).fetchone())

    def toggle_deleted(self, record_id, deleted, revision):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            r=db.execute('SELECT * FROM trades WHERE id=?',(record_id,)).fetchone()
            if not r or r['revision'] != revision:
                raise JournalError('기록이 변경됐습니다. 새로고침 후 다시 시도하세요.')
            db.execute('UPDATE trades SET deleted=?,revision=revision+1,updated_at=? WHERE id=?',
                       (int(deleted), datetime.now(timezone.utc).isoformat(), record_id))
            rows=[dict(x) for x in db.execute('SELECT * FROM trades WHERE account_id=? AND deleted=0 ORDER BY date,created_at,id',(r['account_id'],))]
            self.calculate(rows)

    def set_cash(self, account_id, desired):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            account=db.execute('SELECT * FROM accounts WHERE id=?',(account_id,)).fetchone()
            if not account:
                raise JournalError('계좌를 찾을 수 없습니다.')
            rows=[dict(r) for r in db.execute('SELECT * FROM trades WHERE account_id=? AND deleted=0 ORDER BY date,created_at,id',(account_id,))]
            movement=self.calculate(rows)['cash']
            db.execute('UPDATE accounts SET opening_cash=? WHERE id=?',(int(desired-movement),account_id))

    def import_sheet(self, source):
        """One atomic, idempotent import. Private source is never served as static content."""
        if self.meta('sheet_imported'):
            return
        names={s['name']:s['code'] for s in STOCKS}
        from datetime import date,timedelta
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT COUNT(*) FROM trades').fetchone()[0]:
                raise JournalError('기록이 있는 DB에는 초기 가져오기를 할 수 없습니다.')
            for account in source['accounts']:
                records=[]
                for idx,row in enumerate(account['rows'][1:],2):
                    row=list(row)+[None]*9
                    if not row[0] or not row[2]:
                        continue
                    kind={'매수':'buy','매도':'sell','입금':'deposit','출금':'withdraw'}[row[2]]
                    dt=(date(1899,12,30)+timedelta(days=int(row[0]))).isoformat() if isinstance(row[0],(int,float)) else str(row[0])
                    record={'id':f"import-{account['id']}-{idx:04d}",'account_id':account['id'],'date':dt,
                        'code':names.get(row[1]) if kind in ('buy','sell') else None,'kind':kind,
                        'quantity':int(row[3] or 0),'price':int(row[4] or 0),'fee':int(row[5] or 0),
                        'amount':int(row[6] or 0) if kind in ('deposit','withdraw') else 0,'memo':str(row[7] or '')}
                    if kind in ('buy','sell') and record['code'] is None:
                        raise JournalError('가져올 종목을 찾을 수 없습니다.')
                    now=f"{dt}T00:00:{idx:02d}+09:00"
                    db.execute('''INSERT INTO trades(id,account_id,date,code,kind,quantity,price,fee,amount,memo,created_at,updated_at)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', tuple(record.values())+(now,now))
                    records.append({**record,'created_at':now})
                result=self.calculate(sorted(records,key=lambda r:(r['date'],r['created_at'],r['id'])))
                expected={str(r[1]):r for r in account['summary'][9:] if len(r)>4 and r[1]}
                actual={h['code']:h for h in result['holdings']}
                if set(expected)!=set(actual) or any(actual[c]['quantity']!=int(r[2]) or abs(actual[c]['average']-float(r[4]))>0.01 for c,r in expected.items()):
                    raise JournalError(f"{account['name']}: 시트 보유수량/평단가 대조 실패")
                current_cash=int(account['summary'][2][1] or 0)
                db.execute('UPDATE accounts SET opening_cash=? WHERE id=?',(int(current_cash-result['cash']),account['id']))
                for code,row in expected.items():
                    payload={'code':code,'market':'SHEET','price':int(row[5]),'received_at':None,
                             'trade_at':None,'source':'sheet','change':None,'change_pct':None}
                    db.execute('INSERT OR REPLACE INTO quotes VALUES (?,?,?)',(code,'SHEET',json.dumps(payload)))
            db.execute('INSERT INTO meta VALUES (?,?)',('sheet_imported',datetime.now(timezone.utc).isoformat()))

    def backup(self, target):
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as src, sqlite3.connect(str(target)) as dst:
            src.backup(dst)
