# 내 주식 기록장

HTML/CSS/JavaScript 화면 + AWS Python 서버로 구성된 개인 시세 조회·수기 거래 기록 사이트입니다.
키움 API 호출은 시세 인증, 종목정보(`ka10100`), 기본 시세(`ka10001`), 실시간 체결(`0B`),
예상체결(`0H`), 장 상태(`0s`)로 제한합니다. 주문·잔고·체결내역 API를 호출하지 않습니다.

## 기능

- 삼성전자, 삼성전기, NAVER, SK스퀘어, 삼성에스디에스, SK하이닉스 시세
- 한국시간 NXT 프리마켓 → KRX 정규장 → NXT 애프터마켓 자동 전환
- 거래소가 보낸 장 상태 우선 적용, XKRX 휴장일 캘린더, 특별 운영일 설정
- 두 계좌의 보유수량·평단가·평가손익·실현손익 및 현금잔고
- 날짜·수량·단가·수수료/세금·메모 입력, 기록 수정, 삭제 후 복원, CSV 다운로드
- 1초 화면 갱신 (서버에 보관된 시세 조회), 숨겨진 탭은 조회 일시 중지
- 비밀번호 로그인, 12시간 세션, 기기 간 기록 공유
- SQLite 영구 저장, 동시 수정 감지, 저장 재시도 중복 방지, 일일 백업 30개

공개 저장소에는 개인 거래내역, 시트 원본 데이터, 계좌 API 키, 비밀번호가 없습니다.
구글 시트는 초기 자료로만 가져옵니다. 이후 웹 입력은 원본 시트를 수정하지 않습니다.

## 로컬 실행

Python 3.12 이상을 사용합니다.

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt
export JOURNAL_DB=./data/journal.db
export QUOTE_ENABLED=0
.venv/bin/python -m backend.admin --db ./data/journal.db setup --file ./data/setup.json
.venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8765
```

Windows에서는 `.venv/Scripts/python.exe`와 PowerShell의 `$env:이름='값'`을 사용합니다.
개인 최초 설정 토큰은 해당 PC에서 생성된 `data/setup.json`에만 저장됩니다.
주소 `http://127.0.0.1:8765/#setup=<토큰>`을 본인이 열어 비밀번호를 설정합니다.
실시간 수신을 활성화할 경우 서버 환경변수 `KIWOOM_APP_KEY`, `KIWOOM_SECRET_KEY`와 등록 IP가 필요합니다.

## AWS 운영

- 서비스: `stock-journal` (기존 자동매매와 별도)
- 웹 API: `127.0.0.1:8877`, 외부에서는 Caddy HTTPS를 통해서만 접근
- DB: `/var/lib/stock-journal/journal.db`
- 백업: `/var/lib/stock-journal/backups`, 매일 한국시간 03:05
- 키 환경변수: `/etc/stock-journal.env` (root 전용)
- 최초 비밀번호 설정 토큰: 24시간 만료, 한 번만 사용
- 실행 worker: **반드시 1개** (키움 실시간 세션 1개를 모든 웹 화면에서 공유)
- GitHub Pages 배포 대상: `docs/`만 사용
- 외부 연결: 웹 TLS 포트 443; 80은 HTTPS 전환·인증서 발급에 사용
- 신규 API 키 발급이나 기존 봇 재시작 없이 기존 등록 IP에서 시세 연결

`deploy/`의 서비스·백업·Caddy 설정을 사용합니다. 인증서가 없는 HTTP로 개인 정보를 제공하지 마세요.
최초 설정 링크는 저장소나 공개 문서에 게시하지 마세요. 운영 데이터의 가져오기는 private 파일을 서버에
전송한 후 `python -m backend.admin --db <DB> import --file <private JSON>`으로 실행합니다.
가져오기 전에 시트의 보유수량과 평단가를 검사하며, 기존 DB에 기록이 있으면 재가져오지 않습니다.

시세의 `실시간` 표시는 해당 시장의 거래시간 안에서 WebSocket 연결과 30초 이내 체결 수신을 확인한 경우만 사용합니다.
오래된 체결값, REST 최초 조회값, 시트 입력값은 각각 상태와 시각을 구분합니다.
NXT 미대상·시세 없음은 KRX/시트 이전 가격을 NXT 실시간 값으로 표시하지 않습니다.
정규장 화면은 KRX 기준으로 선택하며, NXT 메인마켓 체결은 별도로 수신·보관합니다.
특별 개장시간은 거래소 실시간 이벤트를 우선 적용하고 필요 시 `deploy/market-overrides.json`에서 설정합니다.

## 계산 기준

매입 원가는 `수량 × 체결단가 + 매수 비용`으로 계산합니다. 추가 매수 후 이동평균 평단가를 적용하고,
매도한 수량만큼 당시 평균 원가를 제거합니다. 실현손익은 `매도금액 - 매도 비용 - 제거 원가`입니다.
현금잔고는 입출금·매수·매도·비용을 반영합니다. 시트의 초기 현금잔고를 보존하도록 초기 잔고를 조정합니다.
평가손익은 미래 매도 수수료·세금을 차감하지 않으며, 5% 목표가는 평단가의 1.05배인 참고값입니다.

## 검증

```sh
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q tests
node --check docs/app.js
```

주문 차단, 인증 전 데이터 차단, 원본 대조 가져오기, 이동평균·비용, 과거 거래 변경 검증,
중복 저장·충돌·삭제 복원·백업, 시장 경계 시각·휴장일, KRX/NXT 분리·이전 가격 표시를 검증합니다.

## 공식 API 자료

- [키움 API 소개·제한](https://openapi.kiwoom.com/intro)
- [키움 인증·IP 등록](https://openapi.kiwoom.com/intro/serviceInfo)
- [키움 공식 명세](https://github.com/Kiwoom-Securities/Kiwoom-REST-API/blob/main/kiwoom/_data/kiwoom_api_spec.json)
- [NXT 거래시간](https://nextrade.co.kr/marketOverview/content.do)
