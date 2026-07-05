# Kimp Trading Bot — 개발 이력

## 프로젝트 개요
- 전략: 업비트 현물 매수 + 비트겟 선물 공매도 동시 진입 (김프 차익거래)
- 방식: 모의매매 (paper trading), 실주문 없음
- 서버: Oracle Cloud Free Tier (Ubuntu 22.04, AMD VM.Standard.E2.1.Micro)
- 자본: 업비트 1억원 + 비트겟 2천만원 (레버리지 5배 = 완전헤지)

---

## 1차 — 초기 세팅 (2026-06-07)

### 전략
- 고정 그리드: 김프 -4.5% ~ -0.5% 구간, spacing 0.5%
- 코인 3개 (BTC, ETH, XRP)
- Oracle Cloud 서버 세팅 및 systemd 서비스 등록

### 주요 작업
- Oracle Cloud 인스턴스 생성 (ARM 용량 부족 → AMD로 변경)
- VCN/서브넷 수동 생성 후 공인 IP 할당
- SSH 키 권한 오류 수정 (`icacls` 명령으로 Windows 권한 설정)
- systemd 서비스 등록 (`/etc/systemd/system/kimp.service`)
- venv 위치 문제 해결: scp 업로드 시 Windows venv 덮어쓰기 → `~/kimp_venv` 분리

---

## 2차 — 그리드 튜닝 (2026-06-14 이전)

### 변경
- 구간: -2.5% ~ -0.5%, spacing 0.25%
- 코인 3개 유지

---

## 3차 — 플로팅 그리드 전환 (2026-06-14)

### 배경
- 벤치마크 전략 분석: 2.96억 투자, 1.04% 월수익, 37,587 거래
- 기존 고정 그리드 한계: 김프 범위 이탈 시 진입 불가

### 전략 변경
- 고정 진입레벨 제거 → 현재 김프에서 즉시 진입 (플로팅 그리드)
- 김프가 진입점 대비 +spacing% 오르면 익절
- 항상 최대 슬롯 유지 (빈 슬롯 생기면 즉시 재진입)

### 코인 확장: 15개
BTC, ETH, XRP, SOL, AVAX, LINK, BCH, SUI, DOT, ATOM, DOGE, TRX, ADA, NEAR, SAND

### 설정
- spacing: 0.1% → 이후 0.2%로 수정
- n_slots: 5개/코인
- leverage: 10배 → 완전헤지 재계산 후 5배로 수정
- 자본: 1억 ÷ 15코인 = 666만원/코인 → 슬롯당 133만원

---

## 4차 — 버그 수정 및 최적화 (2026-06-14 ~ 06-17)

### 수정 사항

**환율 계산 오류**
- 문제: 해외 forex API는 국내 기준보다 약 -13원 낮음 (kimchi premium 왜곡)
- 해결: dunamu/업비트 crix 국내 기준환율만 사용, 해외 API 금지

**비트겟 P&L 환산 오류**
- 문제: 비트겟 USDT 수익을 dunamu 환율로 환산 → 김프 미반영
- 해결: 업비트 USDT 시세(김프 포함)로 환산 (`upbit_usdt_krw`)
  - 실제 경로: 비트겟 USDT → 업비트 USDT 매도 → KRW

**수수료 계산 수정**
- 비트겟 수수료: 0.06% → 0.04% (시장가 taker 기준)
- 슬리피지: 제거 (모의매매)
- 손익분기: spacing 0.18% 이상 → spacing 0.2%로 설정
- 총 수수료: 업비트 0.05%×2 + 비트겟 0.04%×2 = 0.18%

**NameError 수정**
- `_exit_slot()` 시그니처 변경 후 `usd_krw` → `upbit_usdt_krw` 누락 수정

**KeyError: 'grid_top'**
- 서버에 구버전 paper_trader.py 잔존 → 신버전 업로드로 해결

**tick size 문제 발견**
- 저가 코인(DOGE 0.76%/틱, TRX 0.30%/틱)은 spacing 0.2%보다 호가단위가 커서 무의미
- 해결: DOGE, TRX, ADA, NEAR 제거

### 코인 변경: 15개 → 10개
제거: DOGE, TRX, ADA, NEAR, DOT, ATOM, SAND  
추가: UNI (유니스왑), TAO (비트텐서)  
최종: BTC, ETH, XRP, SOL, AVAX, LINK, BCH, SUI, UNI, TAO

### 설정 최종값
- spacing: 0.2%
- n_slots: 5개/코인
- leverage: 5배 (완전헤지)
- 자본: 1억 ÷ 10코인 = 1,000만원/코인 → 슬롯당 200만원
- polling: 10초

---

## 3일 모의매매 결과 분석 (2026-06-17)

### 데이터: trades.csv (2,163건, 구버전 26건 + 신버전 2,137건)

### 신버전 플로팅 그리드 성과 (2,137건)
| 구분 | 건수 | 순수익 |
|------|------|--------|
| 현재 운영 10개 코인 | 571건 | +8.1만원 |
| 제거된 코인 (DOGE 등) | 1,566건 | +444.5만원 |
| **합계** | **2,137건** | **+452.7만원** |

### 현재 10개 코인 일평균
- 190건/일, +2.7만원/일 → 월 약 81만원 (투자금 대비 0.07%)
- 벤치마크 목표: 1.04%/월 → **미달, 튜닝 필요**

### 손익 구조 문제
- gross는 양수이지만 수수료가 커서 BTC/ETH/XRP/SOL은 net 음수
- 원인: spacing 0.1% 시절 진입 거래가 포함됨 (gross < fee)

---

## 다음 튜닝 예정 (2026-06-21)
```
python tune.py 7
```
- spacing / n_slots 조정
- 코인별 수익률 비교 후 교체 여부 검토

---

## 서버 운영 정보
- 주소: oracle cloud (<USER>@<SERVER>)
- 서비스: `sudo systemctl [start|stop|status] kimp`
- 실시간 로그: `sudo journalctl -u kimp -f`
- venv: `~/kimp_venv` (프로젝트 외부)
- 로그 파일: `~/kimp_trading/paper_trading/logs/`
  - `trades.csv`: 거래 기록
  - `trading.log`: 실시간 로그
- CSV 다운로드: `scp -i 키.key <USER>@IP:~/kimp_trading/paper_trading/logs/trades.csv .`
