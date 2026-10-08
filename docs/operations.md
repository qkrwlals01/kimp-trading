# 운영 메모

서버 주소·계정·키 파일 같은 접속 정보는 이 저장소에 두지 않는다.
맥에서 서버 기록을 읽는 도구는 git 에 올리지 않는 `config/settings.py` 의 `SERVER_HOST`, `SERVER_KEY_PATH` 를 쓴다.

## 구성

| 위치 | 하는 일 |
|---|---|
| 서버 (Oracle Cloud Linux VM, systemd) | 모의매매 `kimp` 와 호가 로거 `kimp-quotes` 를 24시간 돌린다. 주문은 내지 않는다 |
| 맥 | 판정 리포트·리플레이·대시보드. 서버에서 기록만 받아 와 분석한다 |

서버는 메모리가 1GB 남짓이라 무거운 분석(일주일치 호가 재생)은 맥에서 한다.
서버에서 돌리면 운영체제가 메모리 부족으로 모의매매 프로세스를 끌 수 있고, 그러면 열린 슬롯이 기록 없이 사라진다.

## 서버 서비스

```bash
sudo systemctl status kimp            # 모의매매 (start / stop / restart 같은 형식)
sudo systemctl status kimp-quotes     # 호가 로거 (10초마다 15코인 호가 기록)
sudo journalctl -u kimp -f            # 실시간 로그
```

- 호가 로거 설치 방법은 `real_trading/kimp-quotes.service` 머리말에 있다
- 가상환경은 프로젝트 폴더 밖에 둔다. 코드를 올릴 때 가상환경을 덮어쓰지 않게 하려는 것이다
- 모의매매의 열린 슬롯은 메모리에만 있다. **판정 기간에는 재시작하지 않는다**

## 기록 파일

| 파일 | 내용 |
|---|---|
| `paper_trading/logs/trades_book.csv` | 현재 전략 거래 기록 (청산 사유 `reason`, 청산 때 환율 `exit_fx` 포함) |
| `paper_trading/logs/trades_book_20260927_v1.csv` | 호가 기준 이전 전략 기록 (09-27 ~ 10-06) |
| `paper_trading/logs/trades.csv` | 체결가 기준 초기 기록 (~07-29, 보존) |
| `paper_trading/logs/trading.log` | 모의매매 실행 로그 (진입 기록 포함) |
| `real_trading/logs/quotes/` | 호가 로그 (일별 CSV, 지난 날짜는 gzip) |

모두 `.gitignore` 대상이다.

## 점검과 분석

```bash
python -m real_trading.quote_logger --status      # 서버에서: 오늘 호가 기록 상태
python -m real_trading.coin_selector --days 7     # 호가 로그로 코인 판정

python tools/weekly_report.py --days 14           # 맥에서: 동기화 → 실제 결과 → 재생 대조 → 비교안 → 판정 기준
python tools/weekly_report.py --no-sync           # 받아 둔 데이터로만
python tools/live_dashboard.py --open             # 맥에서: 실시간 대시보드 (http://127.0.0.1:8765, 서버는 읽기만)
```

판정 리포트 결과는 `reports/` 에 저장되고 git 에 올리지 않는다.

## 코드 배포

서버 폴더는 git 저장소가 아니다. 바뀐 파일만 SSH 로 올리고 서비스를 재시작한다.
재시작 전에 열린 슬롯이 없는지 로그로 확인하고, 옛 코드는 날짜를 붙인 백업 폴더에 남긴다.

## 실거래 실행기 (드라이런까지 검증, 실주문은 사람이 직접)

```bash
python -m real_trading.live_trader --check                  # 읽기 전용 점검 (키·잔고·수수료·비트겟 계정 모드)
python -m real_trading.live_trader                          # 드라이런 (주문 없음)
python -m real_trading.live_trader --live --roundtrip XRP   # 최소 수량 1왕복 시험
python -m real_trading.live_trader --live                   # 실거래 (확인 입력 필요)
```

- 거래소 키는 출금 권한 없이 발급한다
- `logs/STOP` 파일이 있으면 신규 진입을 멈추고, `logs/FLATTEN` 파일이 있으면 열린 슬롯을 모두 청산한다
