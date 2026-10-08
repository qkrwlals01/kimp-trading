"""
실시간 대시보드 — 서버 모의매매를 맥에서 본다
────────────────────────────────────────────────────────────────────
서버에는 아무것도 설치하지 않는다. 10초마다 SSH 로 서버의 기록을 읽기만 한다.
  거래 기록   paper_trading/logs/trades_book.csv
  모의매매 로그 paper_trading/logs/trading.log  ('▶ 진입', 시작 표시, 봇이 마지막으로 본 김프)
  호가 로그   real_trading/logs/quotes/  (처음 한 번 26시간치, 그 뒤로는 새 줄만)
  서비스 상태 systemctl is-active kimp kimp-quotes

열린 슬롯은 서버 메모리에만 있으므로 '진입 로그 − 청산 기록'으로 복원하고, 지금 청산하면 받을 금액을
모의매매와 같은 식으로 계산한다 (tools/weekly_report.py 의 미청산 평가와 같은 방법).
  업비트 진입가 = 슬롯자본 ÷ 수량,  비트겟 진입가 = 업비트 진입가 ÷ ((1 + 진입김프) × 진입 때 환율)
펀딩은 호가 로그의 펀딩비를 보유 시간만큼 누적한 추정치다.
진입 경계는 호가 로그로 다시 계산한 24시간 하위 20% 라서 봇 내부 값과 조금 다를 수 있다.

실행:
      python tools/live_dashboard.py            # http://127.0.0.1:8765
      python tools/live_dashboard.py --open     # 브라우저도 연다
"""

import os, re, sys, csv, json, zlib, time, base64, bisect, shlex, argparse, threading, subprocess, webbrowser
from collections import deque, defaultdict
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from paper_trading.paper_settings import (                       # 서버와 같은 설정 파일
    PAPER_COINS, PAPER_TOTAL_KRW, PAPER_STOP_MARGIN_RATIO, PAPER_TIME_STOP_HOURS,
    PAPER_FEE_ROUND as FEE_RATE, PAPER_FEE_KR, PAPER_FEE_BG, PAPER_BG_REBATE,
)

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
HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_dashboard.html")
KST = timezone(timedelta(hours=9))
HIST_S = 26 * 3600                # 처음 받아 올 호가 기간 (24시간 진입 경계 + 차트 여유)
JUDGE_DAYS = 14                   # 시작부터 판정까지 (CHANGELOG 9차)

# 진입 로그: "[모의/XRP] ▶ 진입  김프=0.55%  목표=0.85%  수량=123.456789  자본=55.6만원"
ENTRY_RE = re.compile(r"\[모의/(\w+)\] ▶ 진입\s+김프=([-\d.]+)%(?:\s+목표=([-\d.]+)%\s+수량=([\d.]+)(?:\s+자본=([\d.]+)만원)?)?")
TICK_RE = re.compile(r"\[모의/(\w+)\] 김프 진입 ([-+\d.]+)% / 청산 ([-+\d.]+)%\s+활성 (\d+)/(\d+)슬롯")
POLL_RE = re.compile(r"\[폴링\] 환율\(dunamu\) ([\d,.]+)\s+업비트USDT ([\d,.]+)")
START_MARK = "플로팅 그리드 시작 (호가 기준)"

# ── 서버에서 실행할 읽기 전용 스크립트 (python3 표준 라이브러리만, 서버는 3.10) ──────────
REMOTE_PY = r'''
import os, sys, json, glob, gzip, time, zlib, base64, bisect, subprocess
from datetime import datetime, timezone
a = json.loads(sys.argv[1])
B = os.path.expanduser("~/kimp_trading")
LOG = B + "/paper_trading/logs/trading.log"
TB = B + "/paper_trading/logs/trades_book.csv"
QD = B + "/real_trading/logs/quotes"
COINS = set(a["coins"])
out = {"now": time.time()}

def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
out["svc"] = {n: sh("systemctl", "is-active", n) for n in ("kimp", "kimp-quotes")}

def ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()

# 모의매매 로그: 지난번 이후 새로 쓰인 '완전한 줄' 중 필요한 것만
off = a["off"]
size = os.path.getsize(LOG) if os.path.exists(LOG) else 0
if size < off:
    off = 0
keys = ("▶ 진입", "플로팅 그리드 시작", "[진입 필터]", "가상손절", "ERROR")
ev = []
if size > off:
    with open(LOG, "rb") as f:
        f.seek(off)
        data = f.read(size - off)
    cut = data.rfind(b"\n") + 1
    ev = [l for l in data[:cut].decode("utf-8", "replace").splitlines() if any(k in l for k in keys)]
    off += cut
out["off"], out["events"] = off, ev
tail = []
if size:
    with open(LOG, "rb") as f:
        f.seek(max(0, size - 6000))
        tail = f.read().decode("utf-8", "replace").splitlines()[1:]
out["tail"] = tail[-12:]

# 거래 기록: 이미 받은 줄 다음부터
rows = []
if os.path.exists(TB):
    with open(TB, encoding="utf-8") as f:
        rows = f.read().splitlines()
out["trades_header"] = rows[0] if rows else ""
out["ntr_total"] = max(len(rows) - 1, 0)
out["trades"] = rows[1 + a["ntr"]:] if a["ntr"] <= out["ntr_total"] else rows[1:]

# 호가: 처음이면 최근 hist 초, 아니면 오늘·어제 파일 끝부분에서 last_ts 이후만
COLS = ("ts", "coin", "up_bid", "up_ask", "bg_bid", "bg_ask", "funding", "fx", "usdt_bid")
def parse(lines, header, since):
    idx = [header.index(c) for c in COLS]
    res = []
    for line in lines:
        p = line.rstrip("\n").split(",")
        if len(p) < len(header) or p[idx[1]] not in COINS:
            continue
        try:
            t = ts(p[idx[0]])
            if t <= since:
                continue
            res.append([round(t, 3), p[idx[1]]] + [float(p[i]) if p[i] else None for i in idx[2:]])
        except (ValueError, IndexError):
            continue
    return res
def opener(fn):
    return gzip.open(fn, "rt", encoding="utf-8") if fn.endswith(".gz") else open(fn, encoding="utf-8")
files = sorted(f for f in glob.glob(QD + "/quotes_*.csv*") if "_v" not in os.path.basename(f))
q = []
if a["hist"]:
    since = time.time() - a["hist"]
    day0 = datetime.fromtimestamp(since, timezone.utc).strftime("%Y%m%d")
    for fn in [f for f in files if os.path.basename(f)[7:15] >= day0]:
        with opener(fn) as fh:
            header = fh.readline().strip().split(",")
            q += parse(fh, header, since)
else:
    for fn in [f for f in files[-2:] if f.endswith(".csv")]:
        with open(fn, "rb") as fh:
            header = fh.readline().decode().strip().split(",")
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 120000))
            chunk = fh.read().decode("utf-8", "replace").splitlines()[1:]
        q += parse(chunk, header, a["last_ts"])
q.sort(key=lambda r: r[0])
out["quotes"] = q

# 받아 둔 호가보다 오래된 미청산 슬롯: 진입 때 환율을 그날 파일에서 찾는다
fx = {}
need = {}
for t in a.get("fx_req", []):
    need.setdefault(datetime.fromtimestamp(t, timezone.utc).strftime("%Y%m%d"), []).append(t)
for day, req in need.items():
    tt, ff = [], []
    for fn in [f for f in files if os.path.basename(f)[7:15] == day]:
        with opener(fn) as fh:
            header = fh.readline().strip().split(",")
            it, ic, ifx = header.index("ts"), header.index("coin"), header.index("fx")
            first = None
            for line in fh:
                p = line.split(",")
                if len(p) < len(header):
                    continue
                if first is None:
                    first = p[ic]
                if p[ic] != first:
                    continue
                try:
                    tt.append(ts(p[it])); ff.append(float(p[ifx]))
                except ValueError:
                    continue
    for t in req:
        k = bisect.bisect_right(tt, t) - 1
        if k >= 0:
            fx[str(t)] = ff[k]
out["fx"] = fx
sys.stdout.write(base64.b64encode(zlib.compress(json.dumps(out).encode(), 6)).decode())
'''


def _kimp(krw: float, usdt: float, fx: float) -> float:
    return (krw / (usdt * fx) - 1) * 100


def _log_ts(line: str):
    """모의매매 로그 시각 (서버 UTC, 초 단위)"""
    try:
        return datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


class Store:
    """서버에서 받아 온 원자료와, 그걸로 계산한 화면 상태."""

    def __init__(self, coins):
        self.lock = threading.Lock()
        self.coins = coins
        self.off = 0                    # trading.log 에서 읽은 바이트 위치
        self.events = []                # 진입·시작·경계·가상손절·오류 줄
        self.tail = []
        self.header = None
        self.trades = []                # 거래 기록 (dict)
        self.quotes = {c: deque() for c in coins}   # [t, up_bid, up_ask, bg_bid, bg_ask, funding, fx, usdt]
        self.last_ts = 0.0
        self.fx_cache = {}              # 진입 시각 → 그때 환율
        self.svc = {}
        self.server_now = None
        self.last_ok = None
        self.error = None
        self.state = b'{"loading": true}'


def _ssh(key: str) -> list:
    return ["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "ControlMaster=auto", "-o", "ControlPath=~/.ssh/cm-%C", "-o", "ControlPersist=300"]


def fetch(store: Store, host: str, key: str, first: bool, fx_req: list) -> dict:
    args = {"coins": store.coins, "off": store.off, "ntr": len(store.trades), "last_ts": store.last_ts,
            "hist": HIST_S if first else 0, "fx_req": fx_req}
    cmd = _ssh(key) + [host, "python3 - " + shlex.quote(json.dumps(args))]
    r = subprocess.run(cmd, input=REMOTE_PY, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[-300:] or f"ssh 종료 코드 {r.returncode}")
    return json.loads(zlib.decompress(base64.b64decode(r.stdout)))


def absorb(store: Store, p: dict):
    if p["off"] < store.off:                       # 로그가 새로 시작됨 (모의매매 교체 등)
        store.events = []
    store.off = p["off"]
    store.events += p["events"]
    store.tail = p["tail"] or store.tail
    if p["ntr_total"] < len(store.trades):        # 거래 기록이 새 파일로 바뀜
        store.trades = []
    if p["trades_header"]:
        store.header = next(csv.reader([p["trades_header"]]))
        store.trades += [dict(zip(store.header, row)) for row in csv.reader(p["trades"]) if row]
    for row in p["quotes"]:
        t, dq = row[0], store.quotes[row[1]]
        if not dq or t > dq[-1][0]:
            dq.append([t] + row[2:])
    if p["quotes"]:
        store.last_ts = max(store.last_ts, p["quotes"][-1][0])
    cutoff = (p["now"] or time.time()) - HIST_S
    for dq in store.quotes.values():
        while dq and dq[0][0] < cutoff:
            dq.popleft()
    store.fx_cache.update({float(k): v for k, v in p["fx"].items()})
    store.svc, store.server_now = p["svc"], p["now"]


# ── 화면 상태 계산 ─────────────────────────────────────────────────

def _entries(events: list) -> tuple:
    starts, ents = [], []
    for line in events:
        t = _log_ts(line)
        if t is None:
            continue
        if START_MARK in line:
            starts.append(t)
            continue
        m = ENTRY_RE.search(line)
        if m:
            ents.append((t, m.group(1), float(m.group(2)),
                         float(m.group(3)) if m.group(3) else None, float(m.group(4)) if m.group(4) else None,
                         float(m.group(5)) * 10000 if m.group(5) else None))
    return starts, ents


def _open_slots(ents: list, closed: list, start: float) -> list:
    """진입 로그에서 청산 기록과 짝지어지지 않은 진입 = 열린 슬롯 (weekly_report.reconstruct_open 과 같은 규칙)"""
    ents = [e for e in ents if e[0] >= start - 1]
    used = [False] * len(ents)
    by = defaultdict(list)
    for i, e in enumerate(ents):
        by[e[1]].append(i)
    for r in closed:
        te = datetime.fromisoformat(r["entry_dt"]).timestamp()
        pe = float(r["entry_premium"])
        hit = next((i for i in by[r["coin"]] if not used[i]
                    and abs(ents[i][0] - te) <= 2 and abs(ents[i][2] - pe) <= 0.0051), None)
        if hit is not None:
            used[hit] = True
    return [ents[i] for i in range(len(ents)) if not used[i]]


def _at(rows, t):
    """t 이전 가장 가까운 호가 행 (없으면 None)"""
    k = bisect.bisect_right([r[0] for r in rows], t) - 1
    return rows[k] if k >= 0 else None


def compute(store: Store) -> dict:
    now = store.server_now or time.time()
    starts, ents = _entries(store.events)
    start = starts[-1] if starts else (min(e[0] for e in ents) if ents else now)
    closed = [r for r in store.trades if datetime.fromisoformat(r["entry_dt"]).timestamp() >= start - 1]
    opens = _open_slots(ents, closed, start)
    cfg0 = next(iter(PAPER_COINS.values()))
    lev, spacing, q_lo, win_h = cfg0["leverage"], cfg0["spacing"], cfg0.get("entry_q"), cfg0.get("window", 24)
    stop_move = (1 - PAPER_STOP_MARGIN_RATIO) / lev          # 가상손절이 나는 비트겟 가격 상승률

    # 봇이 마지막으로 본 김프 (로그 끝)
    bot, bot_t = {}, None
    for line in store.tail:
        m = TICK_RE.search(line)
        if m:
            bot[m.group(1)] = {"entry": float(m.group(2)), "exit": float(m.group(3)),
                               "active": int(m.group(4)), "slots": int(m.group(5))}
            bot_t = _log_ts(line) or bot_t
    errors = [l[:200] for l in store.events if "ERROR" in l]
    # 실제 가상손절 경고만 ("!! 가상손절: 마진비율 …"). 시작 때 전략 설명 줄에도 '가상손절' 이라는 말이 있다
    vstops = [l[:200] for l in store.events if "!! 가상손절" in l and (_log_ts(l) or 0) >= start]

    coins, open_rows, fx_req = {}, [], []
    unreal_total = 0.0
    for c, cfg in PAPER_COINS.items():
        rows = list(store.quotes[c])
        cpg = cfg["upbit_capital"] / cfg["n_slots"]
        card = {"coin": c, "cpg": cpg, "n_slots": cfg["n_slots"], "bot": bot.get(c)}
        if rows:
            last = rows[-1]
            t, ub, ua, bb, ba, fund, fx, usdt = last
            usdt = usdt or fx
            card.update(t=t, entry=_kimp(ua, bb, fx), exit=_kimp(ub, ba, fx), fx=fx, usdt=usdt, funding=fund)
            # 24시간 진입 경계 (하위 entry_q) — 10초 기록마다 굴리고 차트용으로 1분 간격으로 줄인다
            win_s, hist, srt = win_h * 3600, deque(), []
            series, last_min = [], None
            for r in rows:
                k = _kimp(r[2], r[3], r[6])
                hist.append((r[0], k))
                bisect.insort(srt, k)
                while hist[0][0] < r[0] - win_s:
                    del srt[bisect.bisect_left(srt, hist.popleft()[1])]
                th = srt[int(q_lo * (len(srt) - 1))] if q_lo is not None and hist[-1][0] - hist[0][0] >= win_s * 0.25 else None
                mi = int(r[0] // 60)
                if r[0] >= now - 24 * 3600 and mi != last_min:
                    series.append([round(r[0]), round(k, 4), None if th is None else round(th, 4), r[6]])
                    last_min = mi
            card["threshold"] = series[-1][2] if series else None
            card["series"] = series
        # 열린 슬롯 평가 — 지금 청산하면 받을 금액
        slots = []
        for (te, coin, k0, target, qty, cap_log) in [o for o in opens if o[1] == c]:
            r0 = _at(rows, te)
            f0 = r0[6] if r0 else store.fx_cache.get(te)
            if f0 is None:
                fx_req.append(te)
            # 서버 설정이 이 맥의 설정과 다르면(코인 수가 바뀐 채 로컬만 안 바뀜 등) 진입 로그의 자본(0.1만원 단위)을 쓴다
            mismatch = cap_log is not None and abs(cap_log - cpg) > 1000
            s = {"t": te, "k0": k0, "target": target if target is not None else k0 + spacing,
                 "hold_h": (now - te) / 3600,
                 "approx": "슬롯 자본 불일치 · 근사" if mismatch else "진입 환율 조회 중 · 근사" if f0 is None
                           else "수량 기록 없음" if qty is None else ""}
            if rows and qty:
                cpg_s = cap_log if mismatch else cpg
                f0 = f0 or card["fx"]                 # 환율을 아직 못 찾았으면 지금 환율로 근사 (approx 표시)
                ub1, ba1 = rows[-1][1], rows[-1][4]
                up0 = cpg_s / qty
                bg0 = up0 / ((1 + k0 / 100) * f0)
                short = round(cpg_s / f0 / bg0, 6)
                gross = qty * ub1 - cpg_s + short * (bg0 - ba1) * card["usdt"]
                # 펀딩 추정: 호가 기록의 펀딩비를 보유 구간만큼 적분 (기록보다 오래된 구간은 가장 오래된 값으로)
                fund_rate_t, prev = 0.0, None
                for r in rows:
                    if r[0] < te:
                        continue
                    if prev is not None:
                        fund_rate_t += (r[5] or 0) * (r[0] - prev[0]) / (8 * 3600)
                    prev = r
                if rows[0][0] > te:
                    fund_rate_t += (rows[0][5] or 0) * (rows[0][0] - te) / (8 * 3600)
                fund = cpg_s * fund_rate_t
                net = gross - FEE_RATE * cpg_s + fund
                s.update(net=net, fund=fund,
                         now_k=_kimp(ub1, ba1, f0),                # 진입 환율로 잰 지금 청산김프 (익절 판단 값)
                         move=(ba1 / bg0 - 1) * 100,               # 비트겟 가격 변화 (가상손절 기준)
                         stop_at=stop_move * 100)
                unreal_total += net
            slots.append(s)
        slots.sort(key=lambda s: s["t"])
        card["slots"] = slots
        coin_closed = [r for r in closed if r["coin"] == c]
        card["realized"] = sum(float(r["net_pnl"]) for r in coin_closed)
        card["n_closed"] = len(coin_closed)
        card["unreal"] = sum(s.get("net", 0.0) for s in slots)
        card["events"] = [[round(e[0]), e[2]] for e in ents if e[1] == c and e[0] >= now - 24 * 3600]
        card["exits"] = [[round(datetime.fromisoformat(r["exit_dt"]).timestamp()), float(r["exit_premium"]),
                          r.get("reason") or "", float(r["net_pnl"])] for r in coin_closed
                         if datetime.fromisoformat(r["exit_dt"]).timestamp() >= now - 24 * 3600]
        coins[c] = card

    for r in closed:                          # 2026-10-06 이전 기록은 사유 열이 없다 → 김프로 추정
        if not r.get("reason"):
            r["reason"] = "익절" if float(r["exit_premium"]) >= float(r["target_premium"]) - 1e-9 else "손절"
    realized = sum(float(r["net_pnl"]) for r in closed)
    by_reason = defaultdict(lambda: [0, 0.0])
    for r in closed:
        k = r.get("reason") or "?"
        by_reason[k][0] += 1
        by_reason[k][1] += float(r["net_pnl"])
    equity, acc = [[round(start), 0.0]], 0.0
    for r in sorted(closed, key=lambda r: r["exit_dt"]):
        acc += float(r["net_pnl"])
        equity.append([round(datetime.fromisoformat(r["exit_dt"]).timestamp()), round(acc)])
    recent = []
    for r in sorted(closed, key=lambda r: r["exit_dt"], reverse=True)[:15]:
        recent.append({"exit_t": datetime.fromisoformat(r["exit_dt"]).timestamp(), "coin": r["coin"],
                       "reason": r.get("reason") or "", "hold_h": float(r["hold_hours"]),
                       "k0": float(r["entry_premium"]), "net": float(r["net_pnl"])})
    n_open = sum(len(cd["slots"]) for cd in coins.values())
    open_krw = sum(len(cd["slots"]) * cd["cpg"] for cd in coins.values())
    full = sum(cd["cpg"] * cd["n_slots"] for cd in coins.values())
    first_fx = None                            # 모의매매 시작 시점의 은행 환율
    for rows in store.quotes.values():
        r = _at(rows, start) if rows and rows[0][0] <= start else None
        if r:
            first_fx = r[6]
            break
    if first_fx is None:                       # 시작이 받아 둔 호가보다 오래됐으면 서버에서 찾아 온다
        first_fx = store.fx_cache.get(start)
        if first_fx is None:
            fx_req.append(start)
    some = next((cd for cd in coins.values() if cd.get("t")), {})
    return {
        "now": now, "start": start, "judge": start + JUDGE_DAYS * 86400,
        "svc": store.svc, "last_ok": store.last_ok, "error": store.error,
        "bot_t": bot_t, "quote_t": max((cd.get("t", 0) for cd in coins.values()), default=0),
        "strategy": {"coins": list(PAPER_COINS), "spacing": spacing, "entry_q": q_lo, "window": win_h,
                     "tp": cfg0.get("tp", "exit"), "time_stop": PAPER_TIME_STOP_HOURS, "leverage": lev,
                     "stop_move": stop_move * 100, "fee_kr": PAPER_FEE_KR * 100, "fee_bg": PAPER_FEE_BG * 100,
                     "bg_rebate": PAPER_BG_REBATE * 100},
        "capital": PAPER_TOTAL_KRW, "realized": realized, "unreal": unreal_total, "total": realized + unreal_total,
        "n_closed": len(closed), "by_reason": dict(by_reason), "n_open": n_open, "open_krw": open_krw, "full": full,
        "fx": some.get("fx"), "usdt": some.get("usdt"), "fx_start": first_fx,
        "coins": coins, "equity": equity, "recent": recent, "errors": errors[-3:], "vstops": vstops[-3:],
        "fx_pending": sorted(set(fx_req)),
        "cap_mismatch": any(o[5] is not None and abs(o[5] - PAPER_COINS[o[1]]["upbit_capital"] / PAPER_COINS[o[1]]["n_slots"]) > 1000
                            for o in opens if o[1] in PAPER_COINS),
    }


def poller(store: Store, host: str, key: str, interval: float):
    first = True
    while True:
        t0 = time.time()
        try:
            with store.lock:
                fx_req = [t for t in json.loads(store.state).get("fx_pending", []) if t not in store.fx_cache]
            p = fetch(store, host, key, first, fx_req)
            with store.lock:
                absorb(store, p)
                store.last_ok, store.error = time.time(), None
                store.state = json.dumps(compute(store), ensure_ascii=False).encode()
            first = False
        except Exception as e:                       # 연결이 끊겨도 마지막 화면은 유지한다
            with store.lock:
                store.error = f"{type(e).__name__}: {e}"[:300]
                try:
                    st = json.loads(store.state)
                    st.update(error=store.error, last_ok=store.last_ok)
                    store.state = json.dumps(st, ensure_ascii=False).encode()
                except ValueError:
                    pass
        time.sleep(max(1.0, interval - (time.time() - t0)))


def make_handler(store: Store):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/index.html"):
                with open(HTML, "rb") as f:
                    body, ctype = f.read(), "text/html; charset=utf-8"
            elif self.path.startswith("/api/state"):
                with store.lock:
                    body, ctype = store.state, "application/json; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):           # 요청마다 찍지 않음
            pass
    return Handler


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--key", default=KEY)
    ap.add_argument("--interval", type=float, default=10, help="서버에서 받아 오는 간격(초)")
    ap.add_argument("--open", action="store_true", help="브라우저로 열기")
    a = ap.parse_args()

    store = Store(list(PAPER_COINS))
    threading.Thread(target=poller, args=(store, a.host, a.key, a.interval), daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(store))   # 이 맥에서만 접속 가능
    url = f"http://127.0.0.1:{a.port}"
    print(f"실시간 대시보드: {url}   (서버에서 {a.interval:g}초마다 읽기 전용으로 받아 옴, Ctrl+C 로 종료)")
    if a.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
