"""
판정 리포트 — 모의매매 결과를 한 번에 판정한다
────────────────────────────────────────────────────────────────────
서버에서 데이터만 받아 오고 분석은 맥에서 한다. 서버는 메모리가 1GB 남짓이라 일주일치
호가를 올리는 분석을 돌리면 운영체제가 모의매매 프로세스를 강제 종료할 수 있고,
그러면 미청산 슬롯이 기록 없이 사라진다.

하는 일:
  1. 동기화     서버의 호가 로그(바뀐 부분만), 거래 기록 trades_book.csv, 모의매매 진입 로그
  2. 실제 결과  실현 손익 + 미청산 슬롯 평가손익 + 환율 노출분. 미청산 슬롯은 '진입 로그 − 청산 기록'으로 복원
  3. 대조       같은 기간을 재생해 실제와 비교 → 재생 결과를 믿어도 되는가
  4. 개선안     paper_trading/variants.py 의 PRESETS 와 --variant 로 준 설정을 같은 호가로 재생
  5. 기준 점검  판정 기준 통과 여부 (기준값은 옵션으로 조정)
  6. 코인 선정  real_trading/coin_selector 판정 (기본 7일)

미청산 평가손익 = 지금 청산하면 받을 금액 (실현 손익과 같은 식, 수수료·펀딩 반영)
  진입 호가는 진입 로그의 수량·진입김프와 진입 때 환율로 복원한다.
  2026-09-30 까지는 김프 차이 × 슬롯자본으로 근사했는데, 청산 김프를 지금 환율로 다시 계산하는
  탓에 진입 뒤 환율이 움직인 만큼 틀렸다.

환율 노출분 = Σ 열린 슬롯 금액 × 은행 환율 변화율
  헤지 포지션 손익 ≈ 슬롯 금액 × (환율 변화율 + 김프 변화)라서 열어 둔 금액만큼 원/달러에 노출된다.
  '환율 제외' = 합계 − 환율 노출분. 환율 운으로 좋아 보이는 설정을 고르지 않도록 함께 본다.
  환율은 5분 중앙값을 쓴다 (피드가 70초 동안 0.5% 튄 적이 있다. paper_trading/replay.py 참고).
  한국 가격이 환율을 늦게 따라가면 환율분과 김프분이 반대 부호로 함께 커져서, 짧은 기간에는 잡음이 크다.

실행:
      python tools/weekly_report.py                         # 동기화 후 리포트 (개선안 묶음 포함)
      python tools/weekly_report.py --no-sync               # 받아 둔 데이터로
      python tools/weekly_report.py --days 7                # 시작부터 7일까지만 (그 시각 기준 미청산 평가)
      python tools/weekly_report.py --no-presets --variant "tp=entry,spacing=0.4" --variant "cap=0.3"
      python tools/weekly_report.py --min-coins 3 --max-coin-share 0.5 --max-day-share 0.5

결과: 화면 출력 + reports/report_YYYYMMDD_HHMM.txt   (reports/ 는 git 에 올리지 않는다)
"""

import sys, os, re, io, csv, bisect, argparse, subprocess, contextlib, unicodedata
from collections import defaultdict
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from paper_trading import replay as rp
from paper_trading import variants as va
from paper_trading.paper_settings import PAPER_TOTAL_KRW, PAPER_BITGET_KRW
from real_trading import coin_selector as cs

def _server() -> tuple:
    """서버 접속 정보는 git 에 올리지 않는 config/settings.py 에서 읽는다 (SERVER_HOST, SERVER_KEY_PATH)."""
    try:
        from config import settings as s
    except ImportError:
        s = None
    host = getattr(s, "SERVER_HOST", "") if s else ""
    key = os.path.expanduser(getattr(s, "SERVER_KEY_PATH", "") if s else "")
    return host, key


HOST, KEY = _server()
REMOTE = "~/kimp_trading"
REPORTS = os.path.join(ROOT, "reports")
DATA = os.path.join(REPORTS, "data")
KST = timezone(timedelta(hours=9))
W = 118                                                   # 출력 줄 폭
START_MARK = "플로팅 그리드 시작 (호가 기준)"          # paper_trader.run() 시작 로그
# 진입 로그: "[모의/SOL] ▶ 진입  김프=0.04%  목표=0.34%  수량=1.010101  자본=16.7만원"
ENTRY_RE = re.compile(r"\[모의/(\w+)\] ▶ 진입\s+김프=([-\d.]+)%(?:\s+목표=[-\d.]+%\s+수량=([\d.]+))?")


# ── 동기화 ────────────────────────────────────────────────────────────

def _ssh(key: str) -> list:
    return ["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]


def sync(host: str, key: str, data: str):
    qdir = os.path.join(data, "quotes")
    os.makedirs(qdir, exist_ok=True)
    # --delete: 서버가 지난 날짜를 gzip 하면 로컬의 .csv 도 지워야 같은 날이 두 번 읽히지 않는다
    subprocess.run(["rsync", "-az", "--delete", "-e", " ".join(_ssh(key)),
                    f"{host}:{REMOTE}/real_trading/logs/quotes/", qdir + "/"], check=True)
    subprocess.run(["scp", "-q", "-i", key, "-o", "BatchMode=yes",
                    f"{host}:{REMOTE}/paper_trading/logs/trades_book.csv",
                    os.path.join(data, "trades_book.csv")], check=True)
    # trading.log 는 수백 MB 라 진입 기록과 시작 표시만 뽑아 온다 (서버 시간대 UTC)
    cmd = (f"grep -a -F -e '▶ 진입' -e '{START_MARK}' "
           f"{REMOTE}/paper_trading/logs/trading.log")
    out = subprocess.run(_ssh(key) + [host, cmd], check=True, capture_output=True, text=True).stdout
    with open(os.path.join(data, "paper_events.log"), "w", encoding="utf-8") as f:
        f.write(out)


# ── 실제 모의매매 결과 ─────────────────────────────────────────────────

def parse_events(path: str) -> tuple:
    """(시작 시각 목록, 진입 목록[(t, coin, 김프, 수량 또는 None)])"""
    starts, entries = [], []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                t = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
            if START_MARK in line:
                starts.append(t)
                continue
            m = ENTRY_RE.search(line)
            if m:
                entries.append((t, m.group(1), float(m.group(2)),
                                float(m.group(3)) if m.group(3) else None))
    return starts, entries


def reconstruct_open(entries: list, closed: list, start: float) -> tuple:
    """진입 로그에서 청산 기록과 짝지어지지 않는 진입 = 아직 열린 슬롯."""
    ents = [e for e in entries if e[0] >= start]
    used = [False] * len(ents)
    by_coin = defaultdict(list)
    for i, e in enumerate(ents):
        by_coin[e[1]].append(i)
    unmatched = 0
    for r in closed:
        # 로그 김프는 참값을 소수 둘째 자리로, 기록은 넷째 자리로 반올림한 값이라 둘의 차이는 0.00505 이하.
        # 기록 값을 다시 둘째 자리로 반올림해 비교하면 0.2050 → 0.20 vs 로그 0.21 처럼 어긋난다
        te, pe = rp._iso(r["entry_dt"]), float(r["entry_premium"])
        hit = next((i for i in by_coin[r["coin"]] if not used[i]
                    and abs(ents[i][0] - te) <= 2 and abs(ents[i][2] - pe) <= 0.0051), None)
        if hit is None:
            unmatched += 1
        else:
            used[hit] = True
    return [ents[i] for i in range(len(ents)) if not used[i]], unmatched


def funding_curves(snaps: list, coins_cfg: dict) -> dict:
    """코인별 누적 펀딩(8시간 단위 비율의 시간 적분). 슬롯 펀딩 = 슬롯자본 × (끝 − 진입 시점 값)"""
    curves = {c: ([], []) for c in coins_cfg}
    acc, last = defaultdict(float), None
    for t, up, bg, fx, usdt in snaps:
        for c, cfg in coins_cfg.items():
            b = bg.get(cfg["bitget_symbol"])
            if b is None:
                continue
            if last is not None:
                acc[c] += b[5] * (t - last) / (8 * 3600)
            curves[c][0].append(t)
            curves[c][1].append(acc[c])
        last = t
    return curves


def value_open(open_ents: list, snaps: list, coins_cfg: dict) -> tuple:
    """미청산 슬롯을 지금 청산하면 받을 금액 — paper_trader._exit_slot 과 같은 식.
    진입 호가는 진입 로그로 복원한다:
      수량     = 슬롯자본 ÷ 업비트 매도호가              → 업비트 진입가
      진입김프 = 업비트 진입가 ÷ (비트겟 진입가 × 환율) − 1 → 비트겟 진입가 (김프 소수 둘째 자리라 슬롯당 ±10원 안팎)
    반환: ({coin: (개수, 평가손익, [진입김프...], [보유시간h...])}, 김프 차이로 근사한 개수)"""
    t_end, up, bg, fx, usdt = snaps[-1]
    usdt_krw = usdt if usdt else fx
    times = [x[0] for x in snaps]
    curves = funding_curves(snaps, coins_cfg)
    out, approx = {}, 0
    for t, c, prem, qty in open_ents:
        cfg = coins_cfg.get(c)
        if not cfg or cfg["upbit_market"] not in up or cfg["bitget_symbol"] not in bg:
            continue
        cpg = cfg["upbit_capital"] / cfg["n_slots"]
        ub, ba = up[cfg["upbit_market"]][0], bg[cfg["bitget_symbol"]][1]
        fx0 = snaps[max(bisect.bisect_right(times, t) - 1, 0)][3]    # 진입 직전 환율 (로거도 같은 dunamu)
        if qty:
            up0 = cpg / qty
            bg0 = up0 / ((1 + prem / 100) * fx0)
            short = round(cpg / fx0 / bg0, 6)
            gross = qty * (ub - up0) + short * (bg0 - ba) * usdt_krw
        else:   # 수량이 없는 옛 로그 형식 → 김프 차이로 근사 (진입 뒤 환율 변화만큼 틀림)
            approx += 1
            gross = ((ub - ba * fx) / (ba * fx) * 100 - prem) / 100 * cpg
        ts, fs = curves[c]
        k = bisect.bisect_left(ts, t)
        fund = cpg * (fs[-1] - (fs[k] if k < len(fs) else fs[-1]))
        pnl = gross - rp.FEE_RATE * cpg + fund
        n, v, ps, hs = out.get(c, (0, 0.0, [], []))
        out[c] = (n + 1, v + pnl, ps + [prem], hs + [(t_end - t) / 3600])
    return out, approx


def exposure(intervals: list, snaps: list) -> tuple:
    """[(진입 시각, 청산 시각, 슬롯 금액)] → (환율 노출분, 평균 투입액). replay.replay 와 같은 식 (5분 중앙값 환율)."""
    ev = sorted([(a, k) for a, b, k in intervals] + [(b, -k) for a, b, k in intervals])
    fxs = rp.smooth_fx(snaps)
    i, open_krw, fx_pnl, acc = 0, 0.0, 0.0, 0.0
    for j, (t, up, bg, fx, usdt) in enumerate(snaps):
        if j:
            fx_pnl += open_krw * (fxs[j] / fxs[j - 1] - 1)
        while i < len(ev) and ev[i][0] <= t:
            open_krw += ev[i][1]
            i += 1
        acc += open_krw
    return fx_pnl, acc / max(len(snaps), 1)


# ── 판정 기준 ─────────────────────────────────────────────────────────

def row_metrics(summary: dict, open_slots: dict, trades: list,
                fx_pnl: float = 0.0, krw_avg: float = 0.0, capital: float = PAPER_TOTAL_KRW) -> dict:
    per_coin = defaultdict(float)
    for c, v in summary["coin"].items():
        per_coin[c] += v[1]
    for c, v in open_slots.items():
        per_coin[c] += v[1]
    per_day = defaultdict(float)
    for r in trades:
        per_day[datetime.fromisoformat(r["exit_dt"]).astimezone(KST).date()] += float(r["net_pnl"])
    realized = summary["net"]
    unreal = sum(v[1] for v in open_slots.values())
    pos_c = [v for v in per_coin.values() if v > 0]
    pos_d = [v for v in per_day.values() if v > 0]
    rs = summary["reason"]
    return {
        "n": summary["n"], "stops": sum(v[0] for k, v in rs.items() if k != "익절"),
        "tp": rs["익절"][0] if "익절" in rs else 0,
        "tstop": rs["시간손절"][0] if "시간손절" in rs else 0,
        "vstop": rs["가상손절"][0] if "가상손절" in rs else 0,
        "realized": realized, "open_n": sum(v[0] for v in open_slots.values()),
        "unreal": unreal, "total": realized + unreal,
        "fx": fx_pnl, "ex_fx": realized + unreal - fx_pnl, "krw_avg": krw_avg,
        "rate": (realized + unreal) / capital,
        "pos_coins": len(pos_c),
        "coin_share": max(pos_c) / sum(pos_c) if pos_c else 1.0,
        "day_share": max(pos_d) / sum(pos_d) if pos_d else 1.0,
        "per_day": per_day, "per_coin": per_coin,
    }


def verdict(m: dict, a) -> list:
    return [m["total"] > 0,
            m["ex_fx"] > 0,
            m["pos_coins"] >= a.min_coins,
            m["coin_share"] <= a.max_coin_share,
            m["day_share"] <= a.max_day_share]


# ── 출력 ─────────────────────────────────────────────────────────────

class _Tee(io.StringIO):
    """화면에 출력하면서 파일 저장용으로도 모은다."""
    def write(self, s):
        sys.__stdout__.write(s)
        return super().write(s)


def _w(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def _pad(s: str, width: int) -> str:
    """한글은 화면에서 두 칸을 차지해 f-string 폭 지정으로는 줄이 어긋난다."""
    while _w(s) > width:
        s = s[:-1]
    return s + " " * (width - _w(s))


def _r(s: str, width: int) -> str:
    return " " * max(width - _w(s), 0) + s


def _table_row(name: str, x: dict, cfg: dict, thin: float) -> str:
    c0 = next(iter(cfg.values()))
    shape = f"{len(cfg)}×{c0['n_slots']}"
    slot = f"{c0['upbit_capital'] / c0['n_slots'] / 1e4:.1f}만"
    avg = f"{x['krw_avg'] / 1e4:,.0f}만"
    return (f"  {_pad(name, 22)}{shape:>7}{_r(slot, 8)}{x['n']:>6}{x['tp']:>6}{x['tstop']:>6}{x['vstop']:>6}"
            f"{x['total']:>+10,.0f}{x['fx']:>+10,.0f}{x['ex_fx']:>+10,.0f}{_r(avg, 9)}"
            f"{x['rate'] * 100:>+7.2f}%{thin:>9.0%}")


def _kst(t: float) -> str:
    return datetime.fromtimestamp(t, KST).strftime("%m-%d %H:%M")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-sync", action="store_true", help="서버에서 받지 않고 받아 둔 데이터로")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--key", default=KEY)
    ap.add_argument("--data", default=DATA, help="받아 둘 폴더")
    ap.add_argument("--start", default=None, help="분석 시작 (기본: 마지막 모의매매 시작 시각)")
    ap.add_argument("--end", default=None, help="분석 끝 (예: 2026-10-04T07:00:59Z, 기본: 마지막 호가)")
    ap.add_argument("--days", type=float, default=None, help="시작부터 며칠까지만 볼지 (--end 대신, 예: 7)")
    ap.add_argument("--variant", action="append", default=[],
                    help="비교할 개선안 설정 문자열 (여러 번 가능, paper_trading/variants.py 참고)")
    ap.add_argument("--no-presets", action="store_true", help="기본 개선안 묶음(variants.PRESETS)을 빼고 비교")
    ap.add_argument("--min-coins", type=int, default=3, help="기준: 합계 양수인 코인 최소 개수")
    ap.add_argument("--max-coin-share", type=float, default=0.5, help="기준: 이익 중 한 코인 비중 상한")
    ap.add_argument("--max-day-share", type=float, default=0.5, help="기준: 이익 중 하루 비중 상한")
    ap.add_argument("--selector-days", type=int, default=7, help="코인 선정에 쓸 일수")
    a = ap.parse_args()

    specs = ([] if a.no_presets else list(va.PRESETS)) + [(v, v) for v in a.variant]
    pt, trades = rp.load_paper_trader()
    coins_cfg = {c: dict(v) for c, v in pt.COINS.items()}
    try:
        builds = [(name, *va.build(spec, pt.COINS, PAPER_TOTAL_KRW)) for name, spec in specs]
    except ValueError as e:
        print(f"개선안 설정 오류: {e}")
        return 1

    if not a.no_sync:
        print("서버에서 데이터 받는 중...")
        sync(a.host, a.key, a.data)
    qdir = os.path.join(a.data, "quotes")

    tee = _Tee()
    with contextlib.redirect_stdout(tee):
        starts, entries = parse_events(os.path.join(a.data, "paper_events.log"))
        if a.start:
            start = datetime.fromisoformat(a.start.replace("Z", "+00:00")).timestamp()
        elif starts:
            start = starts[-1]
        else:
            print("모의매매 시작 기록을 찾지 못했습니다. --start 로 지정하세요.")
            return 1
        # 끝을 자르면 그 시각 기준으로 다시 본다: 그때까지 청산된 거래만 실현, 그 뒤 청산된 거래는
        # 그 시각에 열려 있던 슬롯으로 복원돼 그 시각 호가로 평가된다
        if a.end:
            end = datetime.fromisoformat(a.end.replace("Z", "+00:00")).timestamp()
        elif a.days:
            end = start + a.days * 86400
        else:
            end = None
        with open(os.path.join(a.data, "trades_book.csv"), encoding="utf-8") as f:
            closed = [r for r in csv.DictReader(f) if rp._iso(r["entry_dt"]) >= start
                      and (end is None or rp._iso(r["exit_dt"]) <= end)]
        if end is not None:
            entries = [e for e in entries if e[0] <= end]

        # 모든 설정이 쓰는 코인의 호가를 한 번에 읽는다. 진입 필터가 있으면 시작 전 기록도 함께
        need = dict(coins_cfg)
        for _, cfg, *_ in builds:
            need.update({c: v for c, v in cfg.items() if c not in need})
        pre = va.history_hours([spec for _, spec in specs], coins_cfg) * 3600
        snaps, _ = rp.load_snapshots(qdir, need, start - pre, end)
        history = [x for x in snaps if x[0] < start]
        snaps = snaps[len(history):]
        if not snaps:
            print(f"분석할 호가가 없습니다: {qdir}")
            return 1
        t0, t1 = snaps[0][0], snaps[-1][0]
        days = (t1 - start) / 86400
        ts_h = pt.TIME_STOP_HOURS
        fx0, fx1 = snaps[0][3], snaps[-1][3]
        u0, u1 = next((x[4] for x in snaps if x[4]), None), snaps[-1][4]
        full = sum(v["upbit_capital"] for v in coins_cfg.values())

        print("=" * W)
        print(f"  판정 리포트 — 작성 {datetime.now(KST):%Y-%m-%d %H:%M} KST")
        print("=" * W)
        print(f"  모의매매 시작 {_kst(start)} KST → {'분석 끝' if end else '마지막 호가'} {_kst(t1)} KST  ({days:.2f}일)")
        cover = len(snaps) / ((t1 - t0) / 10 + 1) * 100
        print(f"  호가 수집률 {cover:.1f}% (시점 {len(snaps):,}개)")
        later = [s for s in starts if s > start and (end is None or s <= end)]
        if later:
            print(f"  ⚠ 분석 구간 안에서 모의매매가 {len(later)}번 재시작됨 — 그때 열려 있던 슬롯은 기록 없이 사라졌을 수 있음")

        # [1] 실제 모의매매
        open_ents, unmatched = reconstruct_open(entries, closed, start)
        open_real, approx = value_open(open_ents, snaps, coins_cfg)
        real = rp.summarize(closed, ts_h)
        spans = [(rp._iso(r["entry_dt"]), rp._iso(r["exit_dt"]), float(r["capital_per_slot"])) for r in closed]
        spans += [(t, t1 + 1, coins_cfg[c]["upbit_capital"] / coins_cfg[c]["n_slots"])
                  for t, c, *_ in open_ents if c in coins_cfg]
        fx_real, krw_real = exposure(spans, snaps)
        mr = row_metrics(real, open_real, closed, fx_real, krw_real)
        print()
        print("[1] 실제 모의매매 (현행 전략)")
        print("-" * W)
        rp.print_summary("실현", real)
        print(f"  [미청산] {mr['open_n']}개, 평가손익 {mr['unreal']:+,.0f}원  (지금 청산하면 받을 금액, 수수료·펀딩 반영)")
        if approx:
            print(f"  ⚠ 미청산 {approx}개는 진입 로그에 수량이 없어 김프 차이로 근사함 (환율 변화만큼 틀림)")
        if unmatched:
            print(f"  ⚠ 청산 기록 {unmatched}건이 진입 로그와 짝지어지지 않음 — 미청산 복원이 부정확할 수 있음")
        stale = sum(1 for o in open_real.values() for h in o[3] if ts_h and h > ts_h + 0.1)
        if stale:
            print(f"  ⚠ 미청산 중 {stale}개가 시간손절({ts_h:g}h)보다 오래 열려 있음 — 이미 청산된 슬롯이 남았을 가능성")
        tot = mr["total"]
        # 짧은 기간을 30일로 늘리면 수십 % 가 찍혀 오해를 부른다 → 3일 이상일 때만
        monthly = (f"  (30일 환산 {tot/PAPER_TOTAL_KRW*100/days*30:+.2f}%)" if days >= 3 else "")
        print(f"  [합계] {tot:+,.0f}원 = 자본 {PAPER_TOTAL_KRW/1e4:,.0f}만원 대비 {tot/PAPER_TOTAL_KRW*100:+.2f}%{monthly}")
        print(f"  [환율] 은행 환율 {fx0:,.1f} → {fx1:,.1f} ({(fx1 / fx0 - 1) * 100:+.2f}%), "
              f"평균 투입 {krw_real:,.0f}원 (최대 {full:,.0f}원의 {krw_real / full * 100:.0f}%)")
        print(f"         합계 중 환율 노출분 {fx_real:+,.0f}원 → 환율 제외 {mr['ex_fx']:+,.0f}원")
        if u0 and u1:
            print(f"         증거금(비트겟 USDT {PAPER_BITGET_KRW:,}원) 평가 변화 {PAPER_BITGET_KRW * (u1 / u0 - 1):+,.0f}원"
                  f" — 모의매매 손익 밖 (업비트 USDT {u0:,.0f} → {u1:,.0f})")
        print()
        print(f"  {'코인':6}{'청산':>5}{'손절':>5}{'실현':>10}{'호가비용':>9}{'미청산':>7}{'평가':>10}{'합계':>10}  미청산 진입김프 (보유h)")
        for c in coins_cfg:
            v = real["coin"].get(c, [0, 0.0, 0.0, 0])
            o = open_real.get(c, (0, 0.0, [], []))
            if v[0] or o[0]:
                det = ", ".join(f"{p:+.2f}({h:.0f})" for p, h in zip(o[2], o[3]))
                print(f"  {c:6}{v[0]:>5}{v[3]:>5}{v[1]:>+10,.0f}{v[2]:>9,.0f}{o[0]:>7}{o[1]:>+10,.0f}"
                      f"{v[1] + o[1]:>+10,.0f}  {det}")
        print()
        print("  일별 실현 (KST): " + "  ".join(f"{d:%m/%d} {v:+,.0f}" for d, v in sorted(mr["per_day"].items())))

        # [2] 재생 대조 — 서버와 같은 코드(PaperCoinGrid)로
        base = rp.run_variant(pt, trades, coins_cfg, snaps, history=history)
        sim = rp.summarize(base["trades"], ts_h)
        ms = row_metrics(sim, base["open"], base["trades"], base["fx_pnl"], base["krw_avg"])
        m = rp.match_trades(closed, base["trades"])
        print()
        print("[2] 재생 대조 — 같은 기간을 현행 설정으로 재생")
        print("-" * W)
        print(f"  {'':8}{'청산':>6}{'손절':>6}{'실현':>11}{'미청산':>7}{'평가':>11}{'합계':>11}{'환율분':>10}{'환율제외':>10}")
        for lab, x in (("실제", mr), ("재생", ms)):
            print(f"  {lab:8}{x['n']:>6}{x['stops']:>6}{x['realized']:>+11,.0f}{x['open_n']:>7}{x['unreal']:>+11,.0f}"
                  f"{x['total']:>+11,.0f}{x['fx']:>+10,.0f}{x['ex_fx']:>+10,.0f}")
        diff_n = (ms["n"] - mr["n"]) / max(mr["n"], 1) * 100
        print(f"  청산 건수 차이 {diff_n:+.0f}%, 짝지어진 거래 {m['matched']}/{m['actual']}건"
              + (f", 그중 청산 사유 일치 {m['same_reason']/m['matched']*100:.0f}%" if m["matched"] else ""))
        print("  (모의매매와 로거는 조회 시점이 달라 개별 거래는 어긋난다. 합계가 가까우면 재생을 믿을 수 있다)")
        print("  재생 손익 구성:")
        rp.print_attribution(sim, base)

        # 개선안 코드가 옵션을 다 껐을 때 서버 코드와 똑같이 거래하는지 매번 확인
        chk = rp.run_variant(pt, trades, coins_cfg, snaps, grid_cls=va.VariantGrid, history=history)
        key = lambda r: (r["coin"], r["entry_dt"], r["exit_dt"], r["net_pnl"])
        same = [key(r) for r in chk["trades"]] == [key(r) for r in base["trades"]]
        thin_base = chk["thin"] / max(chk["orders"], 1)
        if thin_base > 0.01:
            print(f"  ⚠ 현행 주문 {chk['orders']:,}건 중 {thin_base:.0%} 가 최우선 호가 잔량보다 큼 — "
                  "모의매매는 최우선 호가에 전부 체결된다고 보므로 실제 비용은 위보다 크다")

        # [3] 개선안 비교
        rows = [("실제 (현행)", mr), ("재생 (현행)", ms)]
        if builds:
            print()
            print("[3] 개선안 비교 — 같은 호가로 재생 (paper_trading/variants.py, 설정값은 판정 주간을 보기 전에 정함)")
            print("-" * W)
            hdr = [("구성", 7), ("슬롯", 8), ("청산", 6), ("익절", 6), ("시간", 6), ("가상", 6), ("합계", 10),
                   ("환율분", 10), ("환율제외", 10), ("평균투입", 9), ("수익률", 8), ("잔량초과", 9)]
            print("  " + _pad("설정", 22) + "".join(_r(h, w) for h, w in hdr))
            print(_table_row("현행 (재생)", ms, coins_cfg, thin_base))
            margins = {coins_cfg[next(iter(coins_cfg))]["leverage"]: PAPER_BITGET_KRW}
            for name, cfg, tstop, grid, total in builds:
                r = rp.run_variant(pt, trades, cfg, snaps, time_stop=tstop, grid_cls=grid, history=history)
                x = row_metrics(rp.summarize(r["trades"], tstop or ts_h), r["open"], r["trades"],
                                r["fx_pnl"], r["krw_avg"], total)
                rows.append((name, x))
                print(_table_row(name, x, cfg, r["thin"] / max(r["orders"], 1)))
                lev = next(iter(cfg.values()))["leverage"]
                margins.setdefault(lev, total - sum(v["upbit_capital"] for v in cfg.values()))
            print("-" * W)
            print("  구성=코인 수×코인당 슬롯  시간·가상=시간손절·가상손절 건수  합계=실현+미청산 평가  수익률=합계÷총자본")
            print("  환율분=열린 슬롯 금액×은행 환율 변화  평균투입=열린 슬롯 금액의 시간 평균")
            print("  잔량초과=주문 수량이 최우선 호가 잔량보다 컸던 비율 — 클수록 실제 비용이 표보다 크다")
            if u0 and u1:
                print("  증거금(비트겟 USDT) 평가 변화는 표에 없음: " + ", ".join(
                    f"{lv}배 {mg:,.0f}원 → {mg * (u1 / u0 - 1):+,.0f}원" for lv, mg in sorted(margins.items(), reverse=True)))
            if not same:
                print("  ⚠ 옵션을 끈 개선안 코드가 현행과 다르게 거래함 — variants.py 를 고치기 전까지 [3] 을 믿지 말 것")

        # [4] 판정 기준
        print()
        print(f"[4] 판정 기준 — 합계 흑자 / 환율 제외 흑자 / 양수 코인 {a.min_coins}개 이상 / "
              f"한 코인 비중 ≤{a.max_coin_share:.0%} / 하루 비중 ≤{a.max_day_share:.0%}")
        print("-" * W)
        print("  " + _pad("대상", 22) + _r("합계", 10) + _r("환율제외", 10) + _r("양수코인", 9)
              + _r("코인쏠림", 9) + _r("하루쏠림", 9) + "    합계 환율 코인 쏠림 하루 → 판정")
        for name, x in rows:
            ok = verdict(x, a)
            marks = "    ".join("O" if b else "X" for b in ok)
            print(f"  {_pad(name, 22)}{x['total']:>+10,.0f}{x['ex_fx']:>+10,.0f}{x['pos_coins']:>9}"
                  f"{x['coin_share']:>9.0%}{x['day_share']:>9.0%}     {marks}  → {'통과' if all(ok) else '미통과'}")
        if days < 6.5:
            print(f"  ⚠ 기간이 {days:.1f}일뿐입니다. 판정은 7일을 채운 뒤에 하세요.")

        # [5] 코인 선정 — 호가를 새로 읽으므로 재생용 호가는 먼저 놓아 준다
        snaps = history = base = chk = None
        print()
        print(f"[5] 코인 선정 — 최근 {a.selector_days}일 호가 (real_trading/coin_selector)")
        sa = cs.parse_args([])
        sa.dir, sa.days, sa.no_save = qdir, a.selector_days, True
        if end is None:
            data, files = cs.load(sa.dir, sa.days)
        else:   # 끝을 잘랐으면 그 시각까지 selector-days 일치만
            back = sa.days + int((datetime.now(timezone.utc).timestamp() - end) / 86400) + 1
            data, files = cs.load(sa.dir, back)
            lo = end - sa.days * 86400
            data = {c: [r for r in rows if lo <= r[0] <= end] for c, rows in data.items()}
            data = {c: rows for c, rows in data.items() if rows}
        if data:
            t_a = min(r[0][0] for r in data.values() if r)
            t_b = max(r[-1][0] for r in data.values() if r)
            cs.report(cs.evaluate(data, sa), sa, files, (t_b - t_a) / 3600)

    os.makedirs(REPORTS, exist_ok=True)
    cut = f"_to{datetime.fromtimestamp(end, KST):%m%d%H%M}" if end else ""
    out = os.path.join(REPORTS, f"report_{datetime.now(KST):%Y%m%d_%H%M}{cut}.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(tee.getvalue())
    print(f"\n저장: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
