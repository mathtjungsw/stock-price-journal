import asyncio
import csv
import io
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from uuid import UUID
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .auth import Auth
from .journal import Journal, JournalError, NAMES, STOCKS
from .kiwoom import QuoteFeed

class LoginInput(BaseModel):
    password: str = Field(min_length=1,max_length=128)

class SetupInput(LoginInput):
    setup_token: str = Field(min_length=20,max_length=100)

class PasswordInput(BaseModel):
    old_password: str = Field(max_length=128)
    new_password: str = Field(min_length=10,max_length=128)

class TradeInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    id: str
    account_id: str
    date: date
    code: str | None = None
    kind: str
    quantity: int = Field(default=0,ge=0,le=100000000)
    price: int = Field(default=0,ge=0,le=1000000000)
    fee: int = Field(default=0,ge=0,le=1000000000)
    amount: int = Field(default=0,ge=0,le=100000000000000)
    memo: str = Field(default='',max_length=500)

    @model_validator(mode='after')
    def validate_record(self):
        UUID(self.id)
        if self.account_id not in ('joint','soyeon'):
            raise ValueError('계좌를 선택하세요.')
        if self.date > date.today():
            # Use Korean date even when host uses UTC.
            from .market import now_kst
            if self.date > now_kst().date():
                raise ValueError('미래 날짜의 거래는 기록할 수 없습니다.')
        if self.kind in ('buy','sell'):
            if self.code not in NAMES or self.quantity<1 or self.price<1:
                raise ValueError('종목, 수량, 체결단가를 확인하세요.')
            self.amount=0
        elif self.kind in ('deposit','withdraw'):
            if self.amount<1:
                raise ValueError('입출금 금액을 입력하세요.')
            self.code=None
            self.quantity=self.price=self.fee=0
        else:
            raise ValueError('거래구분을 확인하세요.')
        return self

class DeleteInput(BaseModel):
    revision: int = Field(ge=1)
    deleted: bool=True

class CashInput(BaseModel):
    account_id: str
    cash: int = Field(ge=-100000000000000,le=100000000000000)

def create_app(db_path=None,feed_enabled=None):
    db_path=db_path or os.getenv('JOURNAL_DB','data/journal.db')
    journal=Journal(db_path)
    auth=Auth(journal)
    feed=QuoteFeed(journal,enabled=(os.getenv('QUOTE_ENABLED','1')=='1') if feed_enabled is None else feed_enabled)
    write_limits={}

    @asynccontextmanager
    async def lifespan(app):
        await feed.start()
        yield
        await feed.stop()

    app=FastAPI(title='내 주식 기록장',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.journal=journal
    app.state.auth=auth
    app.state.feed=feed
    origins=os.getenv('ALLOWED_ORIGINS','http://127.0.0.1:8765,http://localhost:8765').split(',')
    app.add_middleware(CORSMiddleware,allow_origins=origins,allow_methods=['GET','POST','PUT','PATCH','OPTIONS'],
                       allow_headers=['Authorization','Content-Type'],allow_credentials=False)

    @app.middleware('http')
    async def security(request,call_next):
        if request.method in ('POST','PUT','PATCH'):
            if request.headers.get('origin') not in origins:
                return Response('요청 출처가 허용되지 않았습니다.',status_code=403)
            if int(request.headers.get('content-length','0'))>8192:
                return Response('입력 내용이 너무 큽니다.',status_code=413)
            peer=request.client.host if request.client else 'unknown'
            recent=[x for x in write_limits.get(peer,[]) if x>time.time()-60]
            if len(recent)>=45:
                return Response('잠시 후 다시 시도하세요.',status_code=429)
            write_limits[peer]=recent+[time.time()]
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['X-Frame-Options']='DENY'
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control']='no-store'
        return response

    def authorized(request:Request):
        header=request.headers.get('authorization','')
        token=header.removeprefix('Bearer ') if header.startswith('Bearer ') else ''
        if not token or not auth.valid(token):
            raise HTTPException(401,'로그인이 필요합니다.')
        return token

    def conflict(fn,*args):
        try:
            return fn(*args)
        except (JournalError,ValueError) as e:
            raise HTTPException(409,str(e)) from None

    @app.get('/api/health')
    def health():
        return {'ok':True,'service':'stock-price-journal','version':'1.0.0'}

    @app.get('/api/status')
    def status():
        from .market import market_state,now_kst
        return {'setup_required':not bool(journal.meta('password_hash')),'session':market_state(),
                'server_time':now_kst().isoformat()}

    @app.post('/api/setup')
    def setup(data:SetupInput):
        conflict(auth.setup,data.setup_token,data.password)
        return {'ok':True}

    @app.post('/api/login')
    def login(data:LoginInput,request:Request):
        try:
            return auth.login(data.password,request.client.host if request.client else 'unknown')
        except ValueError as e:
            raise HTTPException(401,str(e)) from None

    @app.post('/api/logout')
    def logout(token=Depends(authorized)):
        auth.logout(token)
        return {'ok':True}

    @app.post('/api/password')
    def change_password(data:PasswordInput,token=Depends(authorized)):
        conflict(auth.change,data.old_password,data.new_password)
        return {'ok':True}

    @app.get('/api/quotes')
    def quotes(token=Depends(authorized)):
        return feed.snapshot()

    @app.get('/api/portfolio')
    def portfolio(account_id:str='joint',token=Depends(authorized)):
        return conflict(journal.portfolio,account_id)

    @app.get('/api/trades')
    def trades(account_id:str='joint',deleted:bool=False,token=Depends(authorized)):
        conflict(journal.account,account_id)
        return {'trades':list(reversed(journal.rows(account_id,deleted)))}

    @app.post('/api/trades')
    def save(data:TradeInput,token=Depends(authorized)):
        return conflict(journal.save,data.model_dump(mode='json'))

    @app.put('/api/trades/{record_id}')
    def update(record_id:str,data:TradeInput,revision:int,token=Depends(authorized)):
        return conflict(journal.save,data.model_dump(mode='json'),record_id,revision)

    @app.patch('/api/trades/{record_id}')
    def remove(record_id:str,data:DeleteInput,token=Depends(authorized)):
        conflict(journal.toggle_deleted,record_id,data.deleted,data.revision)
        return {'ok':True}

    @app.put('/api/cash')
    def cash(data:CashInput,token=Depends(authorized)):
        conflict(journal.set_cash,data.account_id,data.cash)
        return conflict(journal.portfolio,data.account_id)

    @app.get('/api/export')
    def export(account_id:str='joint',token=Depends(authorized)):
        conflict(journal.account,account_id)
        output=io.StringIO()
        writer=csv.writer(output)
        writer.writerow(['거래일자','종목','종목코드','거래구분','수량','체결단가','수수료/세금','입출금금액','메모'])
        labels={'buy':'매수','sell':'매도','deposit':'입금','withdraw':'출금'}
        for row in journal.rows(account_id):
            memo=row['memo']
            if memo.lstrip().startswith(('=','+','-','@')):
                memo="'"+memo
            writer.writerow([row['date'],NAMES.get(row['code'],''),row['code'] or '',labels[row['kind']],
                             row['quantity'],row['price'],row['fee'],row['amount'],memo])
        return Response('\ufeff'+output.getvalue(),media_type='text/csv; charset=utf-8',
                        headers={'Content-Disposition':f'attachment; filename="trades-{account_id}.csv"'})

    static=Path(__file__).resolve().parents[1]/'docs'
    if static.exists():
        app.mount('/',StaticFiles(directory=static,html=True),name='static')
    return app

app=create_app()
