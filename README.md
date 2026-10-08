# 김치프리미엄 차익거래 봇

국내 거래소 현물 매수 + 비트겟 선물 공매도를 동시에 걸어 두 시장의 가격차(김치프리미엄)만
수익으로 취하는 델타-뉴트럴 자동매매 시스템. 현재 **모의매매(paper trading)** 단계이며,
2026-10-06 부터 9차 전략으로 2주 검증 중 (판정 2026-10-20). 실거래 실행기는 드라이런까지 준비됨.

---

## 새 환경(맥북) 세팅

### 1. 클론

```bash
git clone https://github.com/qkrwlals01/kimp-trading.git
cd kimp-trading
```

### 2. 가상환경 + 의존성

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. API 키 설정 (필수 — git에 없음)

`config/settings.py` 는 실제 API 키가 들어 있어 **저장소에 포함되지 않습니다.**
템플릿을 복사해서 직접 채워야 합니다.

```bash
cp config/settings_template.py config/settings.py
```

그 다음 `config/settings.py` 를 열어 아래 5개 값을 실제 키로 교체:

```
UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY
BITGET_ACCESS_KEY / BITGET_SECRET_KEY / BITGET_PASSPHRASE
BITHUMB_ACCESS_KEY / BITHUMB_SECRET_KEY      # 실거래 실행기용 (출금 권한 없이)
```

> 키는 각 거래소에서 재발급하거나, 기존 PC의 `config/settings.py` 를
> USB/비밀번호 관리자 등 **안전한 경로로** 옮기세요. 채팅·메일·git 금지.

### 4. 모의매매 실행 (로컬 테스트)

```bash
python -m paper_trading.paper_trader
```

### 5. 실시간 대시보드 (서버 모의매매를 맥에서 보기)

```bash
python tools/live_dashboard.py --open
# 브라우저: http://127.0.0.1:8765 — 서버에는 설치하는 것 없이 SSH 로 10초마다 기록만 읽음
```

---

## 서버 (Oracle Cloud) 접속

실제 24/7 운영은 로컬이 아니라 오라클 서버에서 돌아갑니다.

- 주소: `<USER>@<SERVER>`
- SSH 키: **git에 없음.** 기존 PC에서 안전하게 옮긴 뒤 권한 설정 필요

```bash
chmod 600 ~/.ssh/<KEY_FILE>          # 맥에서는 600 아니면 접속 거부
ssh -i ~/.ssh/<KEY_FILE> <USER>@<SERVER>
```

### 서버 명령어

```bash
sudo systemctl start kimp          # 모의매매 시작 (stop / status 도 같은 형식)
sudo journalctl -u kimp -f         # 모의매매 실시간 로그
sudo systemctl status kimp-quotes  # 호가 로거 (10초마다 15코인 호가 기록)
```

> 모의매매의 열린 슬롯은 메모리에만 있습니다. 판정 기간 중에는 재시작하지 마세요.

### 코드 배포 / 로그 회수

```bash
# 로컬 → 서버 업로드
scp -i ~/.ssh/<KEY_FILE> paper_trading/paper_settings.py \
    <USER>@<SERVER>:~/kimp_trading/paper_trading/

# 서버 → 로컬: 판정 리포트가 호가 로그·거래 기록을 받아 와 분석
python tools/weekly_report.py --days 14
```

> 서버 가상환경은 프로젝트 **바깥**인 `~/kimp_venv` 에 있습니다.
> systemd 는 `/home/<USER>/kimp_venv/bin/python` 을 직접 호출합니다.

---

## 디렉토리 구조

```
kimp_trading/
├── paper_trading/            # 모의매매 (서버에서 운영 중)
│   ├── paper_trader.py       #   봇 — 플로팅 그리드 + 진입 필터 + 익절 환율 고정 (설정으로 켜고 끔)
│   ├── paper_settings.py     #   코인·자본·전략 옵션·수수료 (한 곳에서 정함)
│   ├── replay.py             #   리플레이: 기록된 호가로 모의매매 코드를 다시 돌림
│   ├── variants.py           #   비교안 설정 (판정 리포트가 현행과 함께 재생)
│   └── logs/                 #   trades_book.csv, trading.log (git 제외)
├── real_trading/
│   ├── quote_logger.py       #   호가 로거 (서버 kimp-quotes)
│   ├── coin_selector.py      #   호가 로그로 코인 판정
│   ├── spread_screener.py    #   전 종목 슬롯 크기 체결비용 스캔
│   ├── live_trader.py        #   실거래 실행기 — 빗썸 현물 + 비트겟 숏 (기본 드라이런)
│   ├── exchanges.py          #   빗썸 API 2.0 + 비트겟 선물 v3/v2 클라이언트
│   └── live_settings.py      #   실거래 슬롯·레버리지·안전장치
├── tools/
│   ├── weekly_report.py      #   판정 리포트 (실제 + 재생 + 비교안 + 판정 기준)
│   └── live_dashboard.py     #   실시간 대시보드 (맥에서 실행)
├── tests/                    # 실행기 시험 (가짜 거래소)
├── data/                     # 분석 스크립트 (백테스트, 김프 시계열 등)
├── core/                     # 초기 그리드 봇 거래소 클라이언트 (옛 코드)
├── tune.py                   # 파라미터 튜닝 (옛 코드)
├── utils/exchange_rate.py    # dunamu 국내기준 환율
└── config/
    ├── settings_template.py  #   키 템플릿 (커밋됨)
    └── settings.py           #   실제 키 (git 제외 — 직접 생성)
```

---

## 전략 요약 (9차, 2026-10-06 ~)

| 항목 | 값 |
|------|-----|
| 김프 공식 | `(국내가 − 비트겟가×은행환율) / (비트겟가×은행환율) × 100` — 진입은 매도호가/매수호가, 청산은 반대 호가 |
| 진입 | 진입 김프가 **최근 24시간 분포의 하위 20%** 이하일 때만, 기존 슬롯과 0.15%p 이상 떨어진 자리 |
| 익절 | 진입 김프 + 0.3%. 청산 김프를 **진입 때 환율로 고정**해 판단 (지금 환율로 재면 환율 변화가 섞여 가짜 익절이 남) |
| 손절 | **시간손절 없음.** 가상손절만 (비트겟 가격이 진입 대비 +16% — 5배 기준) |
| 투입 상한 | 없음 |
| 슬롯 | 코인당 5개 |
| 운영 코인 | **XRP · SUI · LINK** (김프가 자주 출렁여 익절이 잦은 코인 — CHANGELOG 9차) |
| 자본 | 1천만원 (가상, 실자본 규모) — 슬롯당 55.6만원, 레버리지 5배 |
| 폴링 | 10초 |

1주 판정(8차)에서 옛 전략(10코인, 즉시 진입, 시간손절 24h)은 −2.15%/주로 실패했다.
진입 필터가 손실을 없애는 핵심이었고, 시간손절을 없애면 김프가 덜 출렁이는 BTC·ETH·SOL 은 슬롯이 묶여 제외했다.

---

## 수수료와 손익분기

```
빗썸 0.04%(할인 요율)×2 + 비트겟 0.06%×(1 − 환급 50%)×2 = 0.14%   ← 슬롯 왕복 (PAPER_FEE_KR / PAPER_BG_REBATE)
익절폭 0.30% − 0.14% = 0.16%                                      ← 익절 1건 순이익
```

- 모의매매 호가는 업비트, 수수료는 빗썸 기준이다 (실거래는 빗썸에서 할 계획). 빗썸 김프·스프레드는 아직 검증 전
- 모의매매는 최우선 호가에 전량 체결된다고 본다. 잔량을 넘는 주문의 추가 비용은 판정 리포트가 따로 표시한다

### 실거래 실행기 (사용자가 직접 실행)

```bash
python -m real_trading.live_trader --check                  # 읽기 전용 점검 (키·잔고·수수료·비트겟 계정 모드)
python -m real_trading.live_trader                          # 드라이런 (주문 없음, 진입 필터 기록을 쌓음)
python -m real_trading.live_trader --live --roundtrip XRP   # 최소 수량 1왕복 (약 8천원)
python -m real_trading.live_trader --live                   # 실거래 (확인 입력 필요)
```

---

## 진행 이력

| 날짜 | 내용 |
|------|------|
| 2026-06-07 | 고정 그리드 3코인, Oracle Cloud + systemd 세팅 |
| 2026-06-14 | 플로팅 그리드 전환, 15코인 → 10코인 (틱사이즈 필터) |
| 2026-06-20 | 김프 장기 횡보로 슬롯 묶임 → spacing 0.3%, 시간손절 24h 추가 |
| 2026-07-02 | 손실 코인 BTC/XRP/ETH → BSV/AAVE/ATOM 교체 |
| 2026-07-13 | 실거래 준비: 슬리피지 실측 파일럿 작성, 스프레드 문제 발견 |
| 2026-07-29 | 모의매매 중지 (31일 무중단 가동, 재시작 0회) |
| 2026-09-26 | 체결가 착시 발견 → 호가 기준으로 전환, 호가 로거 가동 |
| 2026-09-27 | 가상 자본 1천만원(실자본 규모)으로 재가동, 리플레이·판정 리포트 |
| 2026-09-30 | 익절 신호의 환율 오염 발견 (가짜 익절) |
| 2026-10-06 | 1주 판정: 옛 전략 실패 → 9차 전략(XRP·SUI·LINK, 진입 필터, 시간손절 없음), 수수료 빗썸·비트겟 환급 기준 |
| 2026-10-07 | 실시간 대시보드, 실거래 실행기(빗썸 + 비트겟, 드라이런) |

자세한 내용은 [CHANGELOG.md](CHANGELOG.md) 참고.
