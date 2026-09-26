"""
실행 가능 가격 로거 — 호가 기준 김프 기록
────────────────────────────────────────────────────────────────────
왜 필요한가:
      지금까지의 분석(entry_edge, floating_grid_sim)은 두 거래소의 '마지막 체결가'
      로 김프를 계산했다. 체결가는 매수호가와 매도호가 사이를 오가며 튀기 때문에
      '김프가 가장 낮은 순간'이 실제로는 진입할 수 없는 가격이었을 수 있다
      (호가 튐, bid-ask bounce). 그렇다면 우위가 과대평가된 것이다.

      과거 호가 데이터는 무료로 구할 수 없으므로 지금부터 쌓는다.

무엇을 기록하나 (interval 초마다, 코인별 1행):
      업비트  최우선 매수/매도 호가·잔량 + 서버시각
      비트겟  최우선 매수/매도 호가·잔량 + 서버시각 + 펀딩비
      환율    dunamu (실패 시 upbit crix) — utils/exchange_rate.py
      테더    업비트 KRW-USDT 최우선 호가 (테더 단타 검토용)

      파생값 (분석 편의용. 원자료로 언제든 재계산 가능)
        entry_kimp  진입 김프: 업비트 매도호가에 사고 + 비트겟 매수호가에 숏
        exit_kimp   청산 김프: 업비트 매수호가에 팔고 + 비트겟 매도호가에 숏청산
        mid_kimp    중간가 김프 (참고용)
        entry_kimp - exit_kimp = 그 순간 즉시 왕복하면 잃는 호가 비용

      up_ts, bg_ts 는 각 거래소 응답에 담긴 시각이다. 업비트는 '호가창이 마지막으로
      바뀐 시각'이라 거래가 드문 코인은 호가가 그대로인 동안 차이가 자연히 커진다.
      차이가 크다고 곧 낡은 데이터는 아니다. 두 거래소 조회는 동시에 보내고
      둘 다 도착해야 기록하므로, 수집 시점 자체의 어긋남은 수백 ms 수준이다.

      업비트 호가단위는 가격 구간별로 계단식이다. 가격이 구간 경계를 넘으면
      1틱 비용이 급변한다 (예: DOGE 98원 → 132원, 100원 경계를 넘으며 0.1% → 0.75%).
      코인 적격성은 고정이 아니므로 이 기록으로 주기적으로 다시 판정해야 한다.

출력:
      real_trading/logs/quotes/quotes_YYYYMMDD.csv   (UTC 날짜, 지난 날짜는 gzip)
      코인 15개·10초 기준 하루 약 20MB, gzip 후 약 4MB.

실행:
      python -m real_trading.quote_logger                  # 상시 기록
      python -m real_trading.quote_logger --once           # 1회 조회만 (기록 안 함)
      python -m real_trading.quote_logger --status         # 오늘 기록 상태 점검
      python -m real_trading.quote_logger --status --date 20260926
      python -m real_trading.quote_logger --coins BTC ETH SOL --interval 5

서버 상시 실행: real_trading/kimp-quotes.service (systemd) 참고
"""

import sys, os, csv, glob, gzip, shutil, time, signal, argparse, logging, statistics
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests
from utils.exchange_rate import get_usd_krw

UPBIT = "https://api.upbit.com/v1"
BITGET = "https://api.bitget.com/api/v2"
TIMEOUT = 5

DEFAULT_COINS = [
    # 스크리너 통과 (저비용)
    "BTC", "ETH", "XRP", "SOL", "AVAX", "LINK", "DOGE", "NEAR",
    # 비교군 — 모의매매 운영 코인 중 고비용
    "SUI", "UNI", "BCH", "TAO", "AAVE", "ATOM", "BSV",
]

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "quotes")

FIELDS = ["ts", "coin",
          "up_bid", "up_ask", "up_bid_sz", "up_ask_sz", "up_ts",
          "bg_bid", "bg_ask", "bg_bid_sz", "bg_ask_sz", "bg_ts", "funding",
          "fx", "usdt_bid", "usdt_ask",
          "entry_kimp", "exit_kimp", "mid_kimp"]

log = logging.getLogger("quote_logger")
_stop = False

# 두 거래소를 동시에 조회하므로 세션을 거래소별로 분리 (Session 은 스레드 간 공유 비권장)
_up = requests.Session()
_bg = requests.Session()


def _handle_stop(signum, frame):
    global _stop
    _stop = True
    log.info("종료 신호 수신 — 현재 주기를 마치고 종료")


# ── 조회 ──────────────────────────────────────────────────────────

def fetch_upbit(markets: list) -> dict:
    """업비트 최우선 호가. {market: (bid, ask, bid_sz, ask_sz, server_ts_ms)}"""
    r = _up.get(f"{UPBIT}/orderbook", params={"markets": ",".join(markets)}, timeout=TIMEOUT)
    r.raise_for_status()
    out = {}
    for ob in r.json():
        u = ob["orderbook_units"][0]
        out[ob["market"]] = (float(u["bid_price"]), float(u["ask_price"]),
                             float(u["bid_size"]), float(u["ask_size"]), int(ob["timestamp"]))
    return out


def fetch_bitget() -> dict:
    """비트겟 USDT 선물 전 종목 최우선 호가. {symbol: (bid, ask, bid_sz, ask_sz, ts_ms, funding)}"""
    r = _bg.get(f"{BITGET}/mix/market/tickers", params={"productType": "USDT-FUTURES"},
                timeout=TIMEOUT)
    r.raise_for_status()
    out = {}
    for t in r.json().get("data") or []:
        try:
            out[t["symbol"]] = (float(t["bidPr"]), float(t["askPr"]),
                                float(t["bidSz"]), float(t["askSz"]),
                                int(t["ts"]), float(t.get("fundingRate") or 0))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def kimp(krw: float, usdt: float, fx: float) -> float:
    base = usdt * fx
    return (krw - base) / base * 100 if base > 0 else float("nan")


def snapshot(coins: list, pool: ThreadPoolExecutor) -> list:
    """한 시점의 전 코인 호가. 한 거래소라도 실패하면 예외 — 반쪽짜리 행은 남기지 않는다."""
    markets = [f"KRW-{c}" for c in coins] + ["KRW-USDT"]
    fu = pool.submit(fetch_upbit, markets)
    fb = pool.submit(fetch_bitget)
    up, bg = fu.result(), fb.result()
    fx = get_usd_krw()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    usdt = up.get("KRW-USDT", (None, None, 0, 0, 0))

    rows = []
    for c in coins:
        u, b = up.get(f"KRW-{c}"), bg.get(f"{c}USDT")
        if not u or not b:
            continue
        ub, ua, ubs, uas, uts = u
        bb, ba, bbs, bas, bts, fr = b
        rows.append({
            "ts": ts, "coin": c,
            "up_bid": ub, "up_ask": ua, "up_bid_sz": ubs, "up_ask_sz": uas, "up_ts": uts,
            "bg_bid": bb, "bg_ask": ba, "bg_bid_sz": bbs, "bg_ask_sz": bas, "bg_ts": bts,
            "funding": fr, "fx": fx, "usdt_bid": usdt[0], "usdt_ask": usdt[1],
            "entry_kimp": round(kimp(ua, bb, fx), 5),
            "exit_kimp": round(kimp(ub, ba, fx), 5),
            "mid_kimp": round(kimp((ub + ua) / 2, (bb + ba) / 2, fx), 5),
        })
    return rows


def validate(coins: list) -> tuple:
    """두 거래소에 모두 상장된 코인만 남긴다."""
    krw = {m["market"] for m in _up.get(f"{UPBIT}/market/all", timeout=10).json()}
    bg = set(fetch_bitget())
    ok = [c for c in coins if f"KRW-{c}" in krw and f"{c}USDT" in bg]
    return ok, [c for c in coins if c not in ok]


# ── 저장 ──────────────────────────────────────────────────────────

def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _path(out_dir: str, day: str) -> str:
    return os.path.join(out_dir, f"quotes_{day}.csv")


def append(rows: list, out_dir: str, day: str):
    p = _path(out_dir, day)
    new = not os.path.exists(p)
    with open(p, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)


def gzip_file(p: str):
    if not os.path.exists(p) or os.path.exists(p + ".gz"):
        return
    with open(p, "rb") as src, gzip.open(p + ".gz", "wb") as dst:
        shutil.copyfileobj(src, dst)
    os.remove(p)
    log.info("압축 완료: %s.gz", os.path.basename(p))


# ── 실행 모드 ──────────────────────────────────────────────────────

def run(coins: list, interval: int, out_dir: str) -> int:
    os.makedirs(out_dir, exist_ok=True)
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    ok, bad = validate(coins)
    if bad:
        log.warning("한쪽 거래소에 없어 제외: %s", bad)
    if not ok:
        log.error("기록할 코인이 없습니다")
        return 1
    log.info("기록 시작 — %d개 코인 %s, %d초 간격, 저장 %s", len(ok), ok, interval, out_dir)

    today = _utc_day()
    for p in glob.glob(os.path.join(out_dir, "quotes_*.csv")):   # 재시작 시 지난 날짜 정리
        if not p.endswith(f"_{today}.csv"):
            gzip_file(p)

    pool = ThreadPoolExecutor(max_workers=2)
    cycles = rows_total = errors = consec = 0
    last_beat = time.time()

    while not _stop:
        # 벽시계 기준 interval 경계에 맞춰 대기 — 조회 시간이 누적돼 주기가 밀리지 않도록
        wait = interval - (time.time() % interval)
        while wait > 0 and not _stop:
            time.sleep(min(1.0, wait))
            wait -= 1.0
        if _stop:
            break

        day = _utc_day()
        if day != today:
            gzip_file(_path(out_dir, today))
            today = day

        try:
            rows = snapshot(ok, pool)
            append(rows, out_dir, today)
            cycles += 1
            rows_total += len(rows)
            consec = 0
        except Exception as e:
            errors += 1
            consec += 1
            log.warning("수집 실패 (%d회 연속): %s", consec, e)
            if consec >= 6:                  # 1분 넘게 연속 실패면 잠시 쉬었다 재시도
                time.sleep(min(60, consec * 5))

        if time.time() - last_beat >= 600:
            log.info("정상 동작 중 — 주기 %d회, 기록 %d행, 오류 %d회", cycles, rows_total, errors)
            last_beat = time.time()

    pool.shutdown(wait=False)
    log.info("종료 — 주기 %d회, 기록 %d행, 오류 %d회", cycles, rows_total, errors)
    return 0


def once(coins: list) -> int:
    ok, bad = validate(coins)
    if bad:
        print(f"제외 (한쪽 거래소에 없음): {bad}")
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = snapshot(ok, pool)
    if not rows:
        print("조회 결과 없음")
        return 1
    r0 = rows[0]
    print(f"시각 {r0['ts']}  환율 {r0['fx']:,.1f}  업비트 USDT {r0['usdt_bid']:,.0f}/{r0['usdt_ask']:,.0f}")
    print(f"{'코인':6}{'진입김프':>10}{'청산김프':>10}{'중간김프':>10}{'즉시왕복비용':>12}"
          f"{'시각차':>9}{'펀딩/8h':>10}")
    print("-" * 69)
    for r in rows:
        skew = abs(r["up_ts"] - r["bg_ts"])
        print(f"{r['coin']:6}{r['entry_kimp']:>9.3f}%{r['exit_kimp']:>9.3f}%{r['mid_kimp']:>9.3f}%"
              f"{r['entry_kimp'] - r['exit_kimp']:>11.4f}%{skew:>7}ms{r['funding']*100:>9.4f}%")
    print("-" * 69)
    print("즉시왕복비용 = 진입김프 − 청산김프 (지금 진입해 바로 청산하면 호가로 잃는 폭, 수수료 별도)")
    return 0


def _open(p: str):
    return gzip.open(p, "rt", encoding="utf-8") if p.endswith(".gz") else open(p, encoding="utf-8")


def status(day: str, interval: int, out_dir: str) -> int:
    cands = [_path(out_dir, day), _path(out_dir, day) + ".gz"]
    p = next((c for c in cands if os.path.exists(c)), None)
    if not p:
        print(f"기록 없음: {cands[0]}")
        return 1

    per, stamps, skews = {}, set(), []
    with _open(p) as f:
        for r in csv.DictReader(f):
            stamps.add(r["ts"])
            try:
                skews.append(abs(int(r["up_ts"]) - int(r["bg_ts"])))
                d = per.setdefault(r["coin"], {"e": [], "x": [], "m": [], "f": []})
                d["e"].append(float(r["entry_kimp"]))
                d["x"].append(float(r["exit_kimp"]))
                d["m"].append(float(r["mid_kimp"]))
                d["f"].append(float(r["funding"]))
            except (ValueError, KeyError):
                continue

    ts = sorted(datetime.fromisoformat(s.replace("Z", "+00:00")) for s in stamps)
    span = (ts[-1] - ts[0]).total_seconds()
    expected = span / interval + 1
    gaps = [(b - a).total_seconds() for a, b in zip(ts, ts[1:])]
    big = [g for g in gaps if g > interval * 2.5]

    print("=" * 76)
    print(f"  호가 로거 상태 — {os.path.basename(p)}  ({os.path.getsize(p)/1e6:.1f}MB)")
    print("=" * 76)
    print(f"  구간   {ts[0]:%H:%M:%S} ~ {ts[-1]:%H:%M:%S} UTC  ({span/3600:.1f}시간)")
    print(f"  주기   {len(ts):,}회 / 예상 {expected:,.0f}회  → 수집률 {len(ts)/expected*100:.1f}%")
    print(f"  공백   {interval*2.5:.0f}초 넘는 끊김 {len(big)}회"
          + (f", 최장 {max(big):.0f}초" if big else ""))
    if skews:
        sk = sorted(skews)
        print(f"  시각차 업비트↔비트겟 중앙값 {statistics.median(sk):,.0f}ms, "
              f"95분위 {sk[int(len(sk)*.95)]:,.0f}ms  (거래 드문 코인은 호가 미변경으로 커짐)")
    print()
    print(f"  {'코인':6}{'행':>7}{'진입 중앙':>11}{'청산 중앙':>11}{'왕복비용 중앙':>13}{'중간 최신':>11}{'펀딩 평균':>11}")
    print("  " + "-" * 70)
    for c in sorted(per, key=lambda k: statistics.median(
            [e - x for e, x in zip(per[k]["e"], per[k]["x"])])):
        d = per[c]
        cost = statistics.median([e - x for e, x in zip(d["e"], d["x"])])
        print(f"  {c:6}{len(d['e']):>7,}{statistics.median(d['e']):>10.3f}%"
              f"{statistics.median(d['x']):>10.3f}%{cost:>12.4f}%{d['m'][-1]:>10.3f}%"
              f"{statistics.mean(d['f'])*100:>10.4f}%")
    print("  " + "-" * 70)
    print("  왕복비용 = 진입김프 − 청산김프. 순마진 기준선 0.12%(시장가)와 비교")
    print("=" * 76)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", nargs="*", default=DEFAULT_COINS)
    ap.add_argument("--interval", type=int, default=10, help="기록 간격(초)")
    ap.add_argument("--out", default=OUT_DIR, help="저장 폴더")
    ap.add_argument("--once", action="store_true", help="1회 조회해 출력만 (기록 안 함)")
    ap.add_argument("--status", action="store_true", help="기록 상태 점검")
    ap.add_argument("--date", default=None, help="--status 대상 날짜 YYYYMMDD (UTC, 기본 오늘)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    coins = [c.upper() for c in args.coins]

    if args.status:
        return status(args.date or _utc_day(), args.interval, args.out)
    if args.once:
        return once(coins)
    return run(coins, args.interval, args.out)


if __name__ == "__main__":
    sys.exit(main())
