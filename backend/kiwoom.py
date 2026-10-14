"""Allowlisted quote client. It cannot call orders, balances, or fills."""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
import httpx
from websockets.asyncio.client import connect
from .journal import STOCKS
from .market import KST, market_state, now_kst

log=logging.getLogger('stock_viewer.quotes')
ALLOWED_REST={'ka10001':'/api/dostk/stkinfo','ka10100':'/api/dostk/stkinfo'}
ALLOWED_TYPES={'0B','0H','0s'}

def number(value, absolute=False):
    try:
        n=float(str(value).replace(',','').strip())
        if n!=n or abs(n)==float('inf'):
            return None
        return abs(n) if absolute else n
    except (ValueError,TypeError):
        return None

class QuoteFeed:
    def __init__(self,journal,enabled=True):
        self.journal=journal
        self.enabled=enabled
        self.key=os.getenv('KIWOOM_APP_KEY','')
        self.secret=os.getenv('KIWOOM_SECRET_KEY','')
        self.token=None
        self.expires=now_kst()
        self.http=None
        self.connected=False
        self.status='disabled' if not enabled else 'connecting'
        self.last_message=None
        self.last_error=None
        self.quotes={}
        self.info={}
        self.expectations={}
        self.market_events={}
        self.update_counter=0
        self.tasks=[]
        self.last_snapshot={}
        self.auth_lock=asyncio.Lock()
        with journal.db() as db:
            for row in db.execute('SELECT * FROM quotes'):
                self.quotes[(row['code'],row['market'])]=json.loads(row['payload'])

    async def start(self):
        if not self.enabled or not self.key or not self.secret:
            self.status='disabled' if not self.enabled else 'unconfigured'
            return
        self.http=httpx.AsyncClient(base_url='https://api.kiwoom.com',timeout=15)
        self.tasks=[asyncio.create_task(self.run()),asyncio.create_task(self.snapshots())]

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        self.persist()
        if self.http:
            await self.http.aclose()

    async def auth(self):
        async with self.auth_lock:
            return await self._auth_locked()

    async def _auth_locked(self):
        if self.token and self.expires>now_kst()+timedelta(minutes=5):
            return self.token
        response=await self.http.post('/oauth2/token',json={'grant_type':'client_credentials','appkey':self.key,'secretkey':self.secret})
        response.raise_for_status()
        data=response.json()
        if str(data.get('return_code',0))!='0' or not data.get('token'):
            raise RuntimeError('키움 시세 인증 실패')
        self.token=data['token']
        self.expires=datetime.strptime(data['expires_dt'],'%Y%m%d%H%M%S').replace(tzinfo=KST)
        return self.token

    async def rest(self,api_id,body):
        if api_id not in ALLOWED_REST:
            raise ValueError('조회 허용 목록 밖의 API입니다.')
        token=await self.auth()
        response=await self.http.post(ALLOWED_REST[api_id],json=body,
            headers={'api-id':api_id,'authorization':f'Bearer {token}','cont-yn':'N','next-key':''})
        response.raise_for_status()
        data=response.json()
        if str(data.get('return_code',0))!='0':
            if '토큰' in str(data.get('return_msg','')):
                self.token=None
            raise RuntimeError('키움 시세 조회 실패')
        await asyncio.sleep(0.65)  # Low-volume bootstrap only, no 1-second REST polling.
        return data

    @staticmethod
    def subscription(items,types):
        if set(types)-ALLOWED_TYPES:
            raise ValueError('시세 전용 구독만 허용됩니다.')
        return {'trnm':'REG','grp_no':'901','refresh':'1','data':[{'item':items,'type':types}]}

    def receive(self,message,received=None):
        received=received or now_kst()
        self.last_message=received.isoformat()
        if message.get('trnm')!='REAL':
            return
        codes={s['code'] for s in STOCKS}
        for event in message.get('data',[]):
            typ=event.get('type')
            values=event.get('values',{})
            item=str(event.get('item',''))
            if typ=='0s':
                state=str(values.get('215',''))
                market='NXT' if state in {'P','Q','R','S','T','U','V'} else 'KRX'
                self.market_events[market]={'state':state,'received_at':received.isoformat()}
                continue
            code=item.split('_')[0].removeprefix('A')
            market='NXT' if item.endswith('_NX') else 'KRX' if '_' not in item else None
            if code not in codes or market is None:
                continue  # Never label integrated (_AL) prices as one exchange.
            price=number(values.get('10'),True)
            if not price or price<=0:
                continue
            clock=str(values.get('20',''))
            trade_at=None
            if len(clock)==6 and clock.isdigit():
                try:
                    trade_at=received.replace(hour=int(clock[:2]),minute=int(clock[2:4]),second=int(clock[4:]),microsecond=0).isoformat()
                except ValueError:
                    pass
            if typ=='0H':
                self.expectations[(code,market)]={'price':price,'received_at':received.isoformat()}
            elif typ=='0B':
                self.quotes[(code,market)]={'code':code,'market':market,'price':int(price),
                    'change':number(values.get('11')),'change_pct':number(values.get('12')),
                    'trade_at':trade_at,'received_at':received.isoformat(),'source':'websocket',
                    'volume':number(values.get('13'),True)}
                self.update_counter+=1

    async def run(self):
        backoff=2
        while True:
            try:
                # Keep market-status subscription alive; prices can be displayed as closed outside sessions.
                token=await self.auth()
                self.status='connecting'
                async with connect('wss://api.kiwoom.com:10000/api/dostk/websocket',ping_interval=None,
                                   open_timeout=20,close_timeout=5,max_size=2**22) as ws:
                    await ws.send(json.dumps({'trnm':'LOGIN','token':token}))
                    while True:
                        msg=json.loads(await asyncio.wait_for(ws.recv(),timeout=30))
                        if msg.get('trnm')=='PING':
                            await ws.send(json.dumps(msg))
                        elif msg.get('trnm')=='LOGIN':
                            if str(msg.get('return_code','0'))!='0':
                                if str(msg.get('return_code')) in {'8005','8031','8103'}:
                                    self.token=None
                                raise RuntimeError('키움 실시간 연결 인증 실패')
                            break
                    items=[s['code'] for s in STOCKS]+[s['code']+'_NX' for s in STOCKS]
                    await ws.send(json.dumps(self.subscription(items,['0B','0H'])))
                    await ws.send(json.dumps(self.subscription([],['0s'])))
                    self.connected=True
                    self.status='connected'
                    self.last_error=None
                    backoff=2
                    async for raw in ws:
                        msg=json.loads(raw)
                        if msg.get('trnm')=='PING':
                            await ws.send(json.dumps(msg))
                            self.last_message=now_kst().isoformat()
                        elif msg.get('trnm')=='REG' and str(msg.get('return_code','0'))!='0':
                            raise RuntimeError('시세 종목 등록 실패')
                        elif msg.get('trnm')=='SYSTEM':
                            raise RuntimeError('키움 실시간 서버 연결 종료')
                        else:
                            self.receive(msg)
                        if self.expires<=now_kst()+timedelta(minutes=3):
                            self.token=None
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Never log response bodies, authorization headers, tokens, or secrets.
                log.warning('실시간 연결 재시도: %s',type(e).__name__)
                self.last_error='시세 연결을 다시 시도하고 있습니다.'
            finally:
                self.connected=False
                self.status='reconnecting'
                self.persist()
            await asyncio.sleep(backoff)
            backoff=min(backoff*2,60)

    async def snapshots(self):
        while True:
            try:
                for stock in STOCKS:
                    code=stock['code']
                    if self.last_snapshot.get(code)!=now_kst().date():
                        info=await self.rest('ka10100',{'stk_cd':code})
                        self.info[code]={'nxt':info.get('nxtEnable')=='Y','state':str(info.get('state',''))}
                        for market in ['KRX']+(['NXT'] if self.info[code]['nxt'] else []):
                            data=await self.rest('ka10001',{'stk_cd':code+('_NX' if market=='NXT' else '')})
                            price=number(data.get('cur_prc'),True)
                            if price and price>0:
                                old=self.quotes.get((code,market),{})
                                if old.get('source')=='websocket' and old.get('received_at','')[:10]==now_kst().date().isoformat():
                                    continue
                                self.quotes[(code,market)]={'code':code,'market':market,'price':int(price),
                                    'change':number(data.get('pred_pre')),'change_pct':number(data.get('flu_rt')),
                                    'trade_at':None,'received_at':now_kst().isoformat(),'source':'snapshot',
                                    'volume':number(data.get('trde_qty'),True)}
                        self.last_snapshot[code]=now_kst().date()
                self.persist()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning('초기 시세 조회 재시도: %s',type(e).__name__)
            await asyncio.sleep(60)

    def persist(self):
        with self.journal.db() as db:
            db.executemany('INSERT OR REPLACE INTO quotes VALUES (?,?,?)',
                          [(c,m,json.dumps(v)) for (c,m),v in self.quotes.items()])

    def snapshot(self,now=None):
        now=now or now_kst()
        session=market_state(now)
        # Today's exchange messages take precedence over assumed ordinary hours.
        # Never carry a previous day's open state into a holiday or the next day.
        events={m:e for m,e in self.market_events.items() if e['received_at'][:10]==now.date().isoformat()}
        krx=events.get('KRX',{}).get('state')
        nxt=events.get('NXT',{}).get('state')
        if session['id'] not in {'holiday','closed','before'}:
            if krx in {'3','2'}:
                session.update(market='KRX',active=True,auction=krx=='2',label='KRX 마감 동시호가' if krx=='2' else 'KRX 정규장')
            elif nxt in {'P','U'}:
                session.update(market='NXT',active=True,auction=False,label='NXT 프리마켓' if nxt=='P' else 'NXT 애프터마켓')
            elif nxt in {'Q','T','V'}:
                session.update(market='NXT',active=False,auction=nxt=='T',label='NXT 개장 전환' if nxt=='T' else '장 전환' if nxt=='Q' else '장 종료')
            elif krx=='0' and session['market']=='KRX':
                session.update(active=False,auction=True,label='KRX 개장 대기 · 동시호가')
            elif krx in {'4','8','9'} and session['market']=='KRX':
                session.update(active=False,auction=False,label='KRX 장 종료')
        rows=[]
        for stock in STOCKS:
            code=stock['code']
            info=self.info.get(code,{})
            desired=session['market']
            if desired:
                q=self.quotes.get((code,desired))
            else:
                options=[self.quotes[(code,m)] for m in ('KRX','NXT') if (code,m) in self.quotes]
                q=max(options,key=lambda x:x.get('received_at') or '') if options else None
            fallback=False
            if not q:
                q=self.quotes.get((code,'KRX')) or self.quotes.get((code,'SHEET'))
                fallback=True
            q=dict(q or {'price':None,'market':None,'source':'missing','trade_at':None,'received_at':None})
            live=False
            age=None
            if q.get('received_at'):
                age=max(0,(now-datetime.fromisoformat(q['received_at'])).total_seconds())
                live=self.connected and session['active'] and not session['auction'] and q['source']=='websocket' and age<30 and q['market']==desired
            label='실시간' if live else '마지막 체결' if q.get('trade_at') else '최근 조회값' if q.get('source')=='snapshot' else '시트 입력값' if q.get('source')=='sheet' else '시세 대기'
            if '거래정지' in info.get('state',''):
                label='거래정지'
            elif desired=='NXT' and info.get('nxt') is False:
                label='NXT 대상 아님'
                fallback=True
            elif desired=='NXT' and fallback and q.get('price') is not None:
                label='NXT 시세 없음 · 이전 가격'
            elif session['auction']:
                label='동시호가 · 마지막 가격'
            exp=self.expectations.get((code,desired))
            if exp and (now-datetime.fromisoformat(exp['received_at'])).total_seconds()>60:
                exp=None
            rows.append({**stock,**q,'live':live,'status':label,'age_seconds':age,
                         'nxt_available':info.get('nxt'),'expected':exp if session['auction'] else None})
        return {'quotes':rows,'session':session,'connection':self.status,'last_message':self.last_message,
                'message':self.last_error,'server_time':now.isoformat(),'updates':self.update_counter}
