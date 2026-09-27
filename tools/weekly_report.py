"""
판정 리포트 — 모의매매 결과를 한 번에 판정한다
────────────────────────────────────────────────────────────────────
서버에서 데이터만 받아 오고 분석은 맥에서 한다. 서버는 메모리가 1GB 남짓이라 일주일치
호가를 올리는 분석을 돌리면 운영체제가 모의매매 프로세스를 강제 종료할 수 있고,
그러면 미청산 슬롯이 기록 없이 사라진다.

하는 일:
  1. 동기화     서버의 호가 로그(바뀐 부분만), 거래 기록 trades_book.csv, 모의매매 진입 로그
  2. 실제 결과  실현 손익 + 미청산 슬롯 평가손익. 미청산 슬롯은 '진입 로그 − 청산 기록'으로 복원
  3. 대조       같은 기간을 재생해 실제와 비교 → 재생 결과를 믿어도 되는가
  4. 설정 비교  --variant 로 준 설정들을 같은 호가로 재생
  5. 기준 점검  판정 기준 통과 여부 (기준값은 옵션으로 조정)
  6. 코인 선정  real_trading/coin_selector 판정 (기본 7일)

미청산 평가손익 = 지금 청산하면 받을 금액
               = (현재 청산김프 − 진입김프) × 슬롯자본 − 왕복 수수료 + 보유 중 펀딩
  실현 손익과 같은 기준이다. 김프 차이로 근사하므로 수 % 오차가 있다.

실행:
      python tools/weekly_report.py                         # 동기화 후 리포트
      python tools/weekly_report.py --no-sync               # 받아 둔 데이터로
      python tools/weekly_report.py --variant time_stop=72 --variant "spacing=0.4,slots=3"
      python tools/weekly_report.py --variant "grid=paper_trading.variants:필터그리드"
      python tools/weekly_report.py --min-coins 3 --max-coin-share 0.5 --max-day-share 0.5

결과: 화면 출력 + reports/report_YYYYMMDD_HHMM.txt   (reports/ 는 git 에 올리지 않는다)
"""

import sys, os, re, io, csv, bisect, argparse, subprocess, contextlib
from collections import defaultdict
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from paper_trading import replay as rp
from paper_trading.paper_settings import PAPER_TOTAL_KRW
from real_trading import coin_selector as cs

HOST = "<USER>@<SERVER>"
KEY = os.path.expanduser("~/.ssh/<KEY_FILE>")
REMOTE = "~/kimp_trading"
REPORTS = os.path.join(ROOT, "reports")
DATA = os.path.join(REPORTS, "data")
KST = timezone(timedelta(hours=9))
START_MARK = "플로팅 그리드 시작 (호가 기준)"          # paper_trader.run() 시작 로그
ENTRY_RE = re.compile(r"\[모의/(\w+)\] ▶ 진입\s+김프=([-\d.]+)%")


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
    """(시작 시각 목록, 진입 목록[(t, coin, 김프)])"""
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
                entries.append((t, m.group(1), float(m.group(2))))
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
        te, pe = rp._iso(r["entry_dt"]), round(float(r["entry_premium"]), 2)
        hit = next((i for i in by_coin[r["coin"]] if not used[i]
                    and abs(ents[i][0] - te) <= 2 and abs(ents[i][2] - pe) <= 0.006), None)
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


def value_open(open_ents: list, snaps: list, coins_cfg: dict) -> dict:
    """미청산 슬롯 평가: {coin: (개수, 평가손익, [진입김프...], [보유시간h...])}"""
    t_end, up, bg, fx, usdt = snaps[-1]
    curves = funding_curves(snaps, coins_cfg)
    out = {}
    for t, c, prem in open_ents:
        cfg = coins_cfg.get(c)
        if not cfg or cfg["upbit_market"] not in up or cfg["bitget_symbol"] not in bg:
            continue
        cpg = cfg["upbit_capital"] / cfg["n_slots"]
        ub, ba = up[cfg["upbit_market"]][0], bg[cfg["bitget_symbol"]][1]
        exit_now = (ub - ba * fx) / (ba * fx) * 100
        ts, fs = curves[c]
        k = bisect.bisect_left(ts, t)
        fund = cpg * (fs[-1] - (fs[k] if k < len(fs) else fs[-1]))
        pnl = (exit_now - prem) / 100 * cpg - rp.FEE_RATE * cpg + fund
        n, v, ps, hs = out.get(c, (0, 0.0, [], []))
        out[c] = (n + 1, v + pnl, ps + [prem], hs + [(t_end - t) / 3600])
    return out


# ── 판정 기준 ─────────────────────────────────────────────────────────

def row_metrics(summary: dict, open_slots: dict, trades: list) -> dict:
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
    return {
        "n": summary["n"], "stops": sum(v[0] for k, v in summary["reason"].items() if k != "익절"),
        "realized": realized, "open_n": sum(v[0] for v in open_slots.values()),
        "unreal": unreal, "total": realized + unreal,
        "pos_coins": len(pos_c),
        "coin_share": max(pos_c) / sum(pos_c) if pos_c else 1.0,
        "day_share": max(pos_d) / sum(pos_d) if pos_d else 1.0,
        "per_day": per_day, "per_coin": per_coin,
    }


def verdict(m: dict, a) -> list:
    return [m["total"] > 0,
            m["pos_coins"] >= a.min_coins,
            m["coin_share"] <= a.max_coin_share,
            m["day_share"] <= a.max_day_share]


# ── 설정 비교 ─────────────────────────────────────────────────────────

def parse_variant(spec: str, pt, base_cfg: dict) -> tuple:
    """'time_stop=72,spacing=0.4' → (이름, coins_cfg, time_stop, grid_cls)"""
    cfg = {c: dict(v) for c, v in base_cfg.items()}
    time_stop, grid_cls = None, None
    for part in [p.strip() for p in spec.split(",") if p.strip()]:
        k, v = part.split("=", 1)
        if k == "time_stop":
            time_stop = float(v)
        elif k == "spacing":
            for x in cfg.values():
                x["spacing"] = float(v)
        elif k == "slots":
            for x in cfg.values():
                x["n_slots"] = int(v)
        elif k == "total":
            for x in cfg.values():
                x["upbit_capital"] = int(v) * x["leverage"] // (x["leverage"] + 1) // len(cfg)
        elif k == "grid":
            import importlib
            mod, cls = v.split(":")
            grid_cls = getattr(importlib.import_module(mod), cls)
        else:
            raise ValueError(f"모르는 설정: {k} (time_stop, spacing, slots, total, grid 중 하나)")
    return spec, cfg, time_stop, grid_cls


# ── 출력 ─────────────────────────────────────────────────────────────

class _Tee(io.StringIO):
    """화면에 출력하면서 파일 저장용으로도 모은다."""
    def write(self, s):
        sys.__stdout__.write(s)
        return super().write(s)


def _kst(t: float) -> str:
    return datetime.fromtimestamp(t, KST).strftime("%m-%d %H:%M")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-sync", action="store_true", help="서버에서 받지 않고 받아 둔 데이터로")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--key", default=KEY)
    ap.add_argument("--data", default=DATA, help="받아 둘 폴더")
    ap.add_argument("--start", default=None, help="분석 시작 (기본: 마지막 모의매매 시작 시각)")
    ap.add_argument("--variant", action="append", default=[], help="비교할 설정 (여러 번 가능)")
    ap.add_argument("--min-coins", type=int, default=3, help="기준: 합계 양수인 코인 최소 개수")
    ap.add_argument("--max-coin-share", type=float, default=0.5, help="기준: 이익 중 한 코인 비중 상한")
    ap.add_argument("--max-day-share", type=float, default=0.5, help="기준: 이익 중 하루 비중 상한")
    ap.add_argument("--selector-days", type=int, default=7, help="코인 선정에 쓸 일수")
    a = ap.parse_args()

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
        with open(os.path.join(a.data, "trades_book.csv"), encoding="utf-8") as f:
            closed = [r for r in csv.DictReader(f) if rp._iso(r["entry_dt"]) >= start]

        pt, trades = rp.load_paper_trader()
        coins_cfg = {c: dict(v) for c, v in pt.COINS.items()}
        snaps, _ = rp.load_snapshots(qdir, coins_cfg, start, None)
        if not snaps:
            print(f"분석할 호가가 없습니다: {qdir}")
            return 1
        t0, t1 = snaps[0][0], snaps[-1][0]
        days = (t1 - start) / 86400
        ts_h = pt.TIME_STOP_HOURS

        print("=" * 100)
        print(f"  판정 리포트 — 작성 {datetime.now(KST):%Y-%m-%d %H:%M} KST")
        print("=" * 100)
        restarts = [s for s in starts if s >= start - 1]
        print(f"  모의매매 시작 {_kst(start)} KST → 마지막 호가 {_kst(t1)} KST  ({days:.2f}일)")
        cover = len(snaps) / ((t1 - t0) / 10 + 1) * 100
        print(f"  호가 수집률 {cover:.1f}% (시점 {len(snaps):,}개)")
        later = [s for s in starts if s > start]
        if later:
            print(f"  ⚠ 분석 구간 안에서 모의매매가 {len(later)}번 재시작됨 — 그때 열려 있던 슬롯은 기록 없이 사라졌을 수 있음")

        # [1] 실제 모의매매
        open_ents, unmatched = reconstruct_open(entries, closed, start)
        open_real = value_open(open_ents, snaps, coins_cfg)
        real = rp.summarize(closed, ts_h)
        mr = row_metrics(real, open_real, closed)
        print()
        print("[1] 실제 모의매매 (현행 전략)")
        print("-" * 100)
        rp.print_summary("실현", real)
        print(f"  [미청산] {mr['open_n']}개, 평가손익 {mr['unreal']:+,.0f}원  (지금 청산 시, 수수료·펀딩 반영)")
        if unmatched:
            print(f"  ⚠ 청산 기록 {unmatched}건이 진입 로그와 짝지어지지 않음 — 미청산 복원이 부정확할 수 있음")
        tot = mr["total"]
        # 짧은 기간을 30일로 늘리면 수십 % 가 찍혀 오해를 부른다 → 3일 이상일 때만
        monthly = (f"  (30일 환산 {tot/PAPER_TOTAL_KRW*100/days*30:+.2f}%)" if days >= 3 else "")
        print(f"  [합계] {tot:+,.0f}원 = 자본 {PAPER_TOTAL_KRW/1e4:,.0f}만원 대비 {tot/PAPER_TOTAL_KRW*100:+.2f}%{monthly}")
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

        # [2] 재생 대조
        base = rp.run_variant(pt, trades, coins_cfg, snaps)
        sim = rp.summarize(base["trades"], ts_h)
        ms = row_metrics(sim, base["open"], base["trades"])
        m = rp.match_trades(closed, base["trades"])
        print()
        print("[2] 재생 대조 — 같은 기간을 현행 설정으로 재생")
        print("-" * 100)
        print(f"  {'':8}{'청산':>6}{'손절':>6}{'실현':>11}{'미청산':>7}{'평가':>11}{'합계':>11}")
        for lab, x in (("실제", mr), ("재생", ms)):
            print(f"  {lab:8}{x['n']:>6}{x['stops']:>6}{x['realized']:>+11,.0f}{x['open_n']:>7}{x['unreal']:>+11,.0f}{x['total']:>+11,.0f}")
        diff_n = (ms["n"] - mr["n"]) / max(mr["n"], 1) * 100
        print(f"  청산 건수 차이 {diff_n:+.0f}%, 짝지어진 거래 {m['matched']}/{m['actual']}건"
              + (f", 그중 청산 사유 일치 {m['same_reason']/m['matched']*100:.0f}%" if m["matched"] else ""))
        print("  (모의매매와 로거는 조회 시점이 달라 개별 거래는 어긋난다. 합계가 가까우면 재생을 믿을 수 있다)")

        # [3] 설정 비교
        rows = [("실제 (현행)", mr), ("재생 (현행)", ms)]
        if a.variant:
            print()
            print("[3] 설정 비교 — 같은 호가로 재생")
            print("-" * 100)
            print(f"  {'설정':30}{'청산':>6}{'손절':>6}{'실현':>11}{'미청산':>7}{'평가':>11}{'합계':>11}{'주간%':>8}")
            print(f"  {'현행':30}{ms['n']:>6}{ms['stops']:>6}{ms['realized']:>+11,.0f}{ms['open_n']:>7}"
                  f"{ms['unreal']:>+11,.0f}{ms['total']:>+11,.0f}{ms['total']/PAPER_TOTAL_KRW*100:>+7.2f}%")
            for spec in a.variant:
                name, cfg, tstop, grid = parse_variant(spec, pt, coins_cfg)
                r = rp.run_variant(pt, trades, cfg, snaps, time_stop=tstop, grid_cls=grid)
                x = row_metrics(rp.summarize(r["trades"], tstop or ts_h), r["open"], r["trades"])
                rows.append((name, x))
                print(f"  {name[:30]:30}{x['n']:>6}{x['stops']:>6}{x['realized']:>+11,.0f}{x['open_n']:>7}"
                      f"{x['unreal']:>+11,.0f}{x['total']:>+11,.0f}{x['total']/PAPER_TOTAL_KRW*100:>+7.2f}%")

        # [4] 판정 기준
        print()
        print(f"[4] 판정 기준 — 합계(실현+평가) 흑자 / 양수 코인 {a.min_coins}개 이상 / "
              f"한 코인 비중 ≤{a.max_coin_share:.0%} / 하루 비중 ≤{a.max_day_share:.0%}")
        print("-" * 100)
        print(f"  {'대상':30}{'합계':>11}{'양수코인':>9}{'코인쏠림':>9}{'하루쏠림':>9}   흑자 코인수 코인쏠림 하루쏠림 → 판정")
        for name, x in rows:
            ok = verdict(x, a)
            marks = "  ".join("O" if b else "X" for b in ok)
            print(f"  {name[:30]:30}{x['total']:>+11,.0f}{x['pos_coins']:>9}{x['coin_share']:>8.0%}{x['day_share']:>9.0%}"
                  f"      {marks}    → {'통과' if all(ok) else '미통과'}")
        if days < 6.5:
            print(f"  ⚠ 기간이 {days:.1f}일뿐입니다. 판정은 7일을 채운 뒤에 하세요.")

        # [5] 코인 선정
        print()
        print(f"[5] 코인 선정 — 최근 {a.selector_days}일 호가 (real_trading/coin_selector)")
        sa = cs.parse_args([])
        sa.dir, sa.days, sa.no_save = qdir, a.selector_days, True
        data, files = cs.load(sa.dir, sa.days)
        if data:
            t_a = min(r[0][0] for r in data.values() if r)
            t_b = max(r[-1][0] for r in data.values() if r)
            cs.report(cs.evaluate(data, sa), sa, files, (t_b - t_a) / 3600)

    os.makedirs(REPORTS, exist_ok=True)
    out = os.path.join(REPORTS, f"report_{datetime.now(KST):%Y%m%d_%H%M}.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(tee.getvalue())
    print(f"\n저장: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
