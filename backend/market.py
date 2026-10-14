"""KRX regular session plus NXT pre/after; all clock values are Asia/Seoul."""
import json
import os
from datetime import datetime, time
from zoneinfo import ZoneInfo
import holidays

KST=ZoneInfo('Asia/Seoul')

def now_kst():
    return datetime.now(KST)

def market_state(now=None):
    now=now or now_kst()
    day=now.date()
    closed=holidays.financial_holidays('XKRX', years=day.year)
    overrides={}
    path=os.getenv('MARKET_OVERRIDES')
    if path:
        with open(path,encoding='utf-8') as f:
            overrides=json.load(f)
    override=overrides.get(day.isoformat(),{})
    if day.weekday()>=5 or day in closed or override.get('closed'):
        return {'id':'holiday','label':'휴장일','market':None,'active':False,'auction':False,'date':day.isoformat()}
    t=now.time().replace(tzinfo=None)
    regular_open=time.fromisoformat(override.get('krx_open','09:00'))
    regular_close=time.fromisoformat(override.get('krx_close','15:30'))
    pre_open=time.fromisoformat(override.get('nxt_pre_open','08:00'))
    pre_close=time.fromisoformat(override.get('nxt_pre_close','08:50'))
    after_open=time.fromisoformat(override.get('nxt_after_open','15:40'))
    after_close=time.fromisoformat(override.get('nxt_after_close','20:00'))
    auction_start=time.fromisoformat(override.get('krx_auction','15:20'))
    if t<pre_open:
        sid,label,market,active,auction='before','장 시작 전',None,False,False
    elif t<pre_close:
        sid,label,market,active,auction='nxt_pre','NXT 프리마켓','NXT',True,False
    elif t<regular_open:
        sid,label,market,active,auction='opening','개장 전환 · 동시호가','KRX',False,True
    elif t<regular_close:
        sid,label,market,active,auction='krx','KRX 정규장','KRX',True,t>=auction_start
        if auction:
            label='KRX 마감 동시호가'
    elif t<after_open:
        sid,label,market,active,auction='transition','NXT 개장 전환','NXT',False,True
    elif t<after_close:
        sid,label,market,active,auction='nxt_after','NXT 애프터마켓','NXT',True,False
    else:
        sid,label,market,active,auction='closed','장 종료',None,False,False
    return {'id':sid,'label':label,'market':market,'active':active,'auction':auction,'date':day.isoformat()}
