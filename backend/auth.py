import hashlib
import hmac
import secrets
import time

def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()

def password_hash(password,salt=None):
    salt=salt or secrets.token_hex(16)
    key=hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex()
    return salt+':'+key

def verify(password,stored):
    try:
        salt,_=stored.split(':',1)
        return hmac.compare_digest(password_hash(password,salt),stored)
    except (ValueError,TypeError):
        return False

class Auth:
    def __init__(self,journal):
        self.journal=journal
        self.failures={}

    def setup(self,token,password):
        if len(password)<10 or len(password)>128:
            raise ValueError('비밀번호는 10~128자로 입력하세요.')
        hashed=password_hash(password)
        with self.journal.db() as db:
            db.execute('BEGIN IMMEDIATE')
            meta={r['key']:r['value'] for r in db.execute('SELECT * FROM meta')}
            if meta.get('password_hash') or float(meta.get('setup_expires',0))<time.time() or not hmac.compare_digest(digest(token),meta.get('setup_hash','')):
                raise ValueError('설정 링크가 만료됐거나 이미 사용됐습니다.')
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('password_hash',hashed))
            db.execute("DELETE FROM meta WHERE key IN ('setup_hash','setup_expires')")

    def login(self,password,ip):
        now=time.time()
        failures=[x for x in self.failures.get(ip,[]) if x>now-900]
        self.failures[ip]=failures
        if len(failures)>=8:
            raise ValueError('로그인 시도가 많습니다. 15분 뒤 다시 시도하세요.')
        if not verify(password,self.journal.meta('password_hash','')):
            failures.append(now)
            raise ValueError('비밀번호를 확인해 주세요.')
        self.failures.pop(ip,None)
        token=secrets.token_urlsafe(32)
        expires=int(now+43200)
        with self.journal.db() as db:
            db.execute('DELETE FROM sessions WHERE expires<?',(int(now),))
            db.execute('INSERT INTO sessions VALUES (?,?)',(digest(token),expires))
        return {'token':token,'expires_at':expires}

    def valid(self,token):
        with self.journal.db() as db:
            r=db.execute('SELECT expires FROM sessions WHERE hash=?',(digest(token),)).fetchone()
        return bool(r and r['expires']>time.time())

    def logout(self,token):
        with self.journal.db() as db:
            db.execute('DELETE FROM sessions WHERE hash=?',(digest(token),))

    def change(self,old,new):
        if not verify(old,self.journal.meta('password_hash','')):
            raise ValueError('현재 비밀번호를 확인해 주세요.')
        if len(new)<10 or len(new)>128:
            raise ValueError('새 비밀번호는 10~128자로 입력하세요.')
        with self.journal.db() as db:
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('password_hash',password_hash(new)))
            db.execute('DELETE FROM sessions')
