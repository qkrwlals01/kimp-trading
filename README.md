# 김치프리미엄 차익거래 봇

업비트 현물 매수 + 비트겟 선물 공매도를 동시에 걸어 두 시장의 가격차(김치프리미엄)만
수익으로 취하는 델타-뉴트럴 자동매매 시스템. 현재 **모의매매(paper trading)** 단계.

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
```

> 키는 각 거래소에서 재발급하거나, 기존 PC의 `config/settings.py` 를
> USB/비밀번호 관리자 등 **안전한 경로로** 옮기세요. 채팅·메일·git 금지.

### 4. 모의매매 실행 (로컬 테스트)

```bash
python -m paper_trading.paper_trader
```

### 5. 대시보드

```bash
python -m paper_trading.dashboard
# 브라우저: http://localhost:5000
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
sudo systemctl start kimp      # 시작
sudo systemctl stop kimp       # 중지
sudo systemctl status kimp     # 상태 확인
sudo journalctl -u kimp -f     # 실시간 로그
```

### 대시보드 (SSH 터널 — VCN 포트 개방 불필요)

```bash
ssh -L 5000:localhost:5000 -i ~/.ssh/<KEY_FILE> <USER>@<SERVER> -N
# 브라우저: http://localhost:5000
```

### 코드 배포 / 로그 회수

```bash
# 로컬 → 서버 업로드
scp -i ~/.ssh/<KEY_FILE> paper_trading/paper_settings.py \
    <USER>@<SERVER>:~/kimp_trading/paper_trading/

# 서버 → 로컬 거래기록 다운로드
scp -i ~/.ssh/<KEY_FILE> \
    <USER>@<SERVER>:~/kimp_trading/paper_trading/logs/trades.csv .
```

> 서버 가상환경은 프로젝트 **바깥**인 `~/kimp_venv` 에 있습니다.
> systemd 는 `/home/<USER>/kimp_venv/bin/python` 을 직접 호출합니다.

---

## 디렉토리 구조

```
kimp_trading/
├── paper_trading/          # 모의매매 (현재 운영 중)
│   ├── paper_trader.py     #   메인 봇 — 플로팅 그리드 전략
│   ├── paper_settings.py   #   코인·자본·spacing 설정
│   ├── dashboard.py        #   Flask 손익 대시보드
│   └── logs/               #   trades.csv (git 제외)
├── real_trading/           # 실거래 준비 (슬리피지 실측 파일럿)
│   ├── real_client.py      #   실제 체결가를 조회하는 주문 클라이언트
│   └── slippage_pilot.py   #   왕복 체결비용 측정 스크립트
├── core/                   # 거래소 API 클라이언트
├── utils/exchange_rate.py  # dunamu 국내기준 환율
├── config/
│   ├── settings_template.py#   키 템플릿 (커밋됨)
│   └── settings.py         #   실제 키 (git 제외 — 직접 생성)
└── tune.py                 # 파라미터 튜닝
```

---

## 전략 요약

| 항목 | 값 |
|------|-----|
| 김프 공식 | `(업비트가 − 비트겟가×환율) / (비트겟가×환율) × 100` |
| 진입 | 고정 레벨 없이 **현재 김프에서 즉시** (플로팅 그리드) |
| 익절 | 진입 김프 + spacing(0.3%) 도달 |
| 시간손절 | 24시간 보유 시 강제 청산 (자본 순환) |
| 슬롯 | 코인당 5개, 빈 슬롯 즉시 재진입 |
| 레버리지 | 5배 (완전헤지: 업비트 현물 = 비트겟 명목가치) |
| 자본 | 업비트 1억 + 비트겟 2천만 (가상) |
| 폴링 | 10초 |

**운영 코인 (10종)**
SOL, AVAX, LINK, BCH, SUI, UNI, TAO, BSV, AAVE, ATOM

> 코인 선정 기준: 업비트 **호가단위 / 가격 < 0.15%** 여야 spacing 0.3% 전략이 성립.
> HBAR·ARB(0.9%/틱), PEPE(2.8%/틱) 등은 1틱이 익절폭보다 커서 제외.

---

## 수수료와 손익분기

```
업비트 0.05%×2 + 비트겟 0.04%×2 = 0.18%   ← 손익분기
spacing 0.30% − 0.18% = 0.12%             ← 건당 순마진
```

**주의:** 모의매매는 슬리피지를 0으로 가정합니다.
실측 결과 호가 스프레드만으로 왕복 0.16~0.20%로, 순마진 0.12%를 넘습니다.
→ 실거래 전 `real_trading/slippage_pilot.py` 로 실제 체결비용 측정이 필수입니다.

```bash
python -m real_trading.slippage_pilot                # 드라이런 (주문 없음)
python -m real_trading.slippage_pilot --live --coin AAVE --capital 50000 --trips 1
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

자세한 내용은 [CHANGELOG.md](CHANGELOG.md) 참고.
