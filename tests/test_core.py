import asyncio
import json
import secrets
import time
from datetime import date, datetime, timedelta
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from backend.app import create_app
from backend.auth import Auth, SESSION_SECONDS, digest, password_hash
from backend.journal import Journal, JournalError, one_month_later, sell_reference_price
from backend.kiwoom import QuoteFeed
from backend.market import KST, market_state

def row(kind='buy',qty=2,price=100,fee=0,day='2026-10-01',code='005930',amount=0):
    return {'id':str(uuid4()),'account_id':'joint','date':day,'code':code,'kind':kind,
            'quantity':qty,'price':price,'fee':fee,'amount':amount,'memo':''}

@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path/'journal.db')

def test_weighted_average_partial_sale_and_fees(journal):
    journal.save(row(qty=2,price=100,fee=10))
    journal.save(row(qty=2,price=200,day='2026-10-02'))
    journal.save(row('sell',qty=1,price=300,fee=5,day='2026-10-03'))
    p=journal.portfolio('joint')
    assert p['holdings'][0]['quantity']==3
    assert p['holdings'][0]['average']==152.5
    assert p['holdings'][0]['cost']==457.5
    assert p['realized']==142.5
    assert p['cash']==-315

def test_cash_adjustment_deposit_and_withdraw(journal):
    journal.save(row('deposit',qty=0,price=0,code=None,amount=1000))
    journal.save(row(qty=2,price=100,fee=10))
    assert journal.portfolio('joint')['cash']==790
    journal.set_cash('joint',500)
    journal.save(row('withdraw',qty=0,price=0,code=None,amount=100,day='2026-10-02'))
    assert journal.portfolio('joint')['cash']==400

def test_oversell_is_rejected_atomically(journal):
    journal.save(row(qty=2))
    with pytest.raises(JournalError):
        journal.save(row('sell',qty=3,day='2026-10-02'))
    assert len(journal.rows('joint'))==1

def test_deleting_past_buy_cannot_break_later_sell(journal):
    bought=journal.save(row())
    journal.save(row('sell',qty=2,day='2026-10-02'))
    with pytest.raises(JournalError):
        journal.toggle_deleted(bought['id'],True,1)
    assert len(journal.rows('joint'))==2

def test_soft_delete_and_restore_revision(journal):
    r=journal.save(row())
    journal.toggle_deleted(r['id'],True,1)
    assert len(journal.rows('joint'))==0
    assert journal.rows('joint',True)[0]['revision']==2
    journal.toggle_deleted(r['id'],False,2)
    assert journal.rows('joint')[0]['revision']==3

def test_idempotent_create_and_conflicting_retry(journal):
    data=row()
    assert journal.save(data)['id']==journal.save(data)['id']
    assert len(journal.rows('joint'))==1
    with pytest.raises(JournalError):
        journal.save({**data,'price':200})

def test_conflicting_edit_does_not_overwrite(journal):
    data=row()
    journal.save(data)
    journal.save({**data,'memo':'first'},data['id'],1)
    with pytest.raises(JournalError):
        journal.save({**data,'memo':'stale'},data['id'],1)
    assert journal.rows('joint')[0]['memo']=='first'

def test_backup_contains_committed_records(journal,tmp_path):
    journal.save(row())
    journal.backup(tmp_path/'backup.db')
    assert len(Journal(tmp_path/'backup.db').rows('joint'))==1

@pytest.mark.parametrize('clock,market,active,auction',[
    ('07:59:59',None,False,False),('08:00:00','NXT',True,False),
    ('08:49:59','NXT',True,False),('08:50:00','KRX',False,True),
    ('09:00:00','KRX',True,False),('15:20:00','KRX',True,True),
    ('15:30:00','NXT',False,True),('15:40:00','NXT',True,False),
    ('19:59:59','NXT',True,False),('20:00:00',None,False,False)])
def test_market_boundaries(clock,market,active,auction):
    state=market_state(datetime.fromisoformat('2026-10-14T'+clock).replace(tzinfo=KST))
    assert (state['market'],state['active'],state['auction'])==(market,active,auction)

@pytest.mark.parametrize('day',['2026-10-17','2026-10-18','2026-10-09','2026-12-31'])
def test_closed_calendar(day):
    assert market_state(datetime.fromisoformat(day+'T10:00').replace(tzinfo=KST))['id']=='holiday'

def test_no_broker_order_or_account_api_can_be_called(journal):
    feed=QuoteFeed(journal,False)
    for api_id in ['kt10000','kt10001','kt00018','ka10004','anything']:
        with pytest.raises(ValueError):
            asyncio.run(feed.rest(api_id,{}))
    with pytest.raises(ValueError):
        feed.subscription(['005930'],['00','04'])

def test_distinct_krx_nxt_real_ticks_and_integrated_rejection(journal):
    feed=QuoteFeed(journal,False)
    now=datetime(2026,10,14,10,0,1,tzinfo=KST)
    values={'10':'-100','11':'-2','12':'-1.96','20':'100000'}
    feed.receive({'trnm':'REAL','data':[{'type':'0B','item':'005930','values':values},
        {'type':'0B','item':'005930_NX','values':{**values,'10':'+110'}},
        {'type':'0B','item':'005930_AL','values':{**values,'10':'+999'}}]},now)
    feed.connected=True
    krx=feed.snapshot(now)['quotes'][0]
    assert krx['price']==100 and krx['market']=='KRX' and krx['live']
    assert krx['change']==-2 and krx['change_pct']==-1.96
    assert feed.quotes[('005930','NXT')]['price']==110
    assert len(feed.quotes)==2

def test_stale_price_never_claims_realtime(journal):
    feed=QuoteFeed(journal,False)
    now=datetime(2026,10,14,10,0,1,tzinfo=KST)
    feed.receive({'trnm':'REAL','data':[{'type':'0B','item':'005930','values':{'10':'100','20':'100000'}}]},now-timedelta(days=1))
    feed.connected=True
    assert not feed.snapshot(now)['quotes'][0]['live']

def test_nxt_missing_falls_back_with_visible_status(journal):
    feed=QuoteFeed(journal,False)
    now=datetime(2026,10,14,16,0,1,tzinfo=KST)
    feed.quotes[('005930','KRX')]={'price':100,'market':'KRX','source':'snapshot','trade_at':None,'received_at':now.isoformat()}
    q=feed.snapshot(now)['quotes'][0]
    assert q['price']==100 and not q['live'] and 'NXT 시세 없음' in q['status']

def test_exchange_event_handles_delayed_open(journal):
    feed=QuoteFeed(journal,False)
    now=datetime(2026,10,14,9,30,tzinfo=KST)
    feed.receive({'trnm':'REAL','data':[{'type':'0s','item':'','values':{'215':'0'}}]},now)
    session=feed.snapshot(now)['session']
    assert not session['active'] and session['auction']

@pytest.fixture
def client(tmp_path):
    app=create_app(tmp_path/'api.db',False)
    setup_token=secrets.token_urlsafe(32)
    app.state.journal.set_meta('setup_hash',digest(setup_token))
    app.state.journal.set_meta('setup_expires',time.time()+3600)
    with TestClient(app,headers={'Origin':'http://127.0.0.1:8765'}) as c:
        c.setup_token=setup_token
        yield c

def login(client):
    assert client.post('/api/setup',json={'setup_token':client.setup_token,'password':'Test-password-42'}).status_code==200
    response=client.post('/api/login',json={'password':'Test-password-42'})
    assert response.status_code==200
    client.headers['Authorization']='Bearer '+response.json()['token']

def test_private_data_requires_authentication(client):
    assert client.get('/api/status').status_code==200
    for path in ['/api/portfolio','/api/quotes','/api/trades','/api/export']:
        assert client.get(path).status_code==401
    assert client.get('/.private/sheet-source.json').status_code==404
    assert client.get('/backend/app.py').status_code==404
    assert client.get('/api/health').json()['ok']

def test_setup_one_time_password_revoke_and_storage(client):
    login(client)
    assert client.post('/api/setup',json={'setup_token':client.setup_token,'password':'Test-password-42'}).status_code==409
    assert client.get('/api/portfolio').status_code==200
    assert client.post('/api/password',json={'old_password':'Test-password-42','new_password':'Changed-password-43'}).status_code==200
    assert client.get('/api/portfolio').status_code==401

def test_api_record_persistence_idempotency_and_html_safe_export(client):
    login(client)
    data=row();data['memo']='=1+1'
    assert client.post('/api/trades',json=data).status_code==200
    assert client.post('/api/trades',json=data).status_code==200
    assert len(client.get('/api/trades').json()['trades'])==1
    assert client.get('/api/portfolio').json()['holdings'][0]['quantity']==2
    export=client.get('/api/export')
    assert "'=1+1" in export.text

def test_bad_origin_cannot_mutate(client):
    login(client)
    assert client.post('/api/trades',json=row(),headers={'Origin':'https://attacker.invalid'}).status_code==403

def test_invalid_future_and_unsupported_stock_rejected(client):
    login(client)
    assert client.post('/api/trades',json=row(day='2099-01-01')).status_code==422
    assert client.post('/api/trades',json=row(code='NOT-A-STOCK')).status_code==422
    assert client.post('/api/trades',json=row(qty=0)).status_code==422

@pytest.mark.parametrize('day,expected',[
    ('2026-09-14','2026-10-14'),('2026-10-13','2026-11-13'),
    ('2026-01-31','2026-02-28'),('2024-01-31','2024-02-29'),
    ('2026-12-31','2027-01-31'),('2026-03-31','2026-04-30')])
def test_calendar_month_anniversary(day,expected):
    assert one_month_later(date.fromisoformat(day)).isoformat()==expected

def test_recent_buy_month_eligibility_uses_latest_buy_not_sell():
    records=[row(day='2026-08-01'),row(day='2026-09-14'),row('sell',qty=1,day='2026-10-01')]
    before=Journal.calculate(records,today=date(2026,10,13))['holdings'][0]
    after=Journal.calculate(records,today=date(2026,10,14))['holdings'][0]
    assert before['last_buy_date']=='2026-09-14'
    assert before['next_buy_date']=='2026-10-14'
    assert not before['buy_available'] and after['buy_available']
    records.append(row(day='2026-10-13'))
    assert not Journal.calculate(records,today=date(2026,10,14))['holdings'][0]['buy_available']
    records[-1]['deleted']=1
    assert Journal.calculate(records,today=date(2026,10,14))['holdings'][0]['buy_available']

@pytest.mark.parametrize('cost,qty,expected',[
    ('1624500',6,284500),('11880500',6,2080000),('1904',1,2000),
    ('1905',1,2005),('5000',1,5250),('210',2,111)])
def test_five_percent_reference_never_rounds_below_target(cost,qty,expected):
    from decimal import Decimal
    price=sell_reference_price(Decimal(cost),Decimal(qty))
    assert price==expected
    assert Decimal(price)*qty>=Decimal(cost)*Decimal('1.05')

def test_sale_total_uses_remaining_quantity_and_buy_fees():
    records=[row(qty=4,price=100,fee=20),row('sell',qty=1,price=110,day='2026-10-02')]
    holding=Journal.calculate(records,today=date(2026,10,15))['holdings'][0]
    assert holding['quantity']==3 and holding['average']==105
    assert holding['sell_reference_price']==111
    assert holding['sell_reference_amount']==333

def test_login_remains_valid_for_exactly_seven_days(journal,monkeypatch):
    base=1792000000
    monkeypatch.setattr('backend.auth.time.time',lambda:base)
    journal.set_meta('password_hash',password_hash('Seven-day-test-password'))
    auth=Auth(journal)
    session=auth.login('Seven-day-test-password','test-device')
    assert SESSION_SECONDS==604800
    assert session['expires_at']==base+604800
    monkeypatch.setattr('backend.auth.time.time',lambda:base+604799)
    assert auth.valid(session['token'])
    monkeypatch.setattr('backend.auth.time.time',lambda:base+604800)
    assert not auth.valid(session['token'])

def test_quotes_match_each_accounts_current_holdings(client):
    login(client)
    client.post('/api/trades',json=row(code='005930'))
    data=row(code='035420');data['account_id']='soyeon'
    client.post('/api/trades',json=data)
    assert [q['code'] for q in client.get('/api/quotes?account_id=joint').json()['quotes']]==['005930']
    assert [q['code'] for q in client.get('/api/quotes?account_id=soyeon').json()['quotes']]==['035420']
    client.post('/api/trades',json=row('sell',qty=2,day='2026-10-02'))
    assert client.get('/api/quotes?account_id=joint').json()['quotes']==[]
    assert client.get('/api/quotes?account_id=other').status_code==409
