"""
개선안 — 현행 전략과 같은 호가로 비교할 설정들
────────────────────────────────────────────────────────────────────
전략 규칙(익절 환율 고정, 진입 필터, 시간손절)은 모의매매 코드(paper_trader.PaperCoinGrid)에 있고
설정으로 켜고 끈다. 여기서는 설정 문자열로 그 설정을 바꿔 재생하고, 분석에만 쓰는 두 가지를 더한다.
  - 투입 상한(cap): 전 코인 열린 슬롯 금액 합계 제한. 코인끼리 장부를 함께 봐야 해서 여기 둔다
  - 최우선 호가 잔량보다 큰 주문 수 집계 (모의매매는 전량 최우선 호가에 체결된다고 본다)
설정을 바꾸지 않으면 현행과 똑같이 거래한다. 판정 리포트가 매번 현행 재생과 대조해 확인한다.

설정 문자열 (쉼표로 여러 개, 예: "time_stop=72,cap=0.5")
  tp=entry|exit       익절 판단 환율: 진입 때 고정 / 지금 환율 (2026-09 까지 방식)
                        지금 환율로 다시 계산하면 가격은 그대로인데 환율만 내려도 익절 신호가 난다.
                        진입 환율로 고정한 김프 변화 (1+k₀)(a−b)/b 는 실제 손익 (a−1)−γ(b−1) 과
                        2차 항만큼만 다르다 (a·b: 업비트·비트겟 가격 변화율, γ: USDT 환산 비율).
  entry_q=0.2|none    진입 필터: 진입김프가 최근 window 시간 분포의 하위 q 이하일 때만 / 끔
  window=24           진입 필터 창(시간). 창의 25% 이상 쌓여야 판단. 리플레이는 시작 전 기록으로 미리 채운다
  cap=0.5             투입 상한: 전 코인 열린 슬롯 금액 합계 ≤ 업비트 자본 × cap
  coins=A+B+C         운영 코인. 총자본은 그대로 두고 코인 수로 나눈다 (코인이 적으면 슬롯이 커진다)
  leverage=3          레버리지. 업비트:비트겟 자본 배분(L:1)과 가상손절 거리(5배 +16%, 3배 +27%)가 바뀐다
  time_stop=72|none   시간손절(시간) / 없음
  spacing=0.4  slots=5  total=10000000    현행 설정 덮어쓰기
  grid=모듈:클래스    직접 만든 PaperCoinGrid 상속 클래스

실행:
      python -m paper_trading.replay --start 2026-10-06T00:00:00Z --variant "time_stop=72"
      python tools/weekly_report.py         # 아래 PRESETS 를 현행과 함께 비교
"""

import bisect, importlib
from collections import deque

from paper_trading import paper_trader as pt

# 판정 리포트가 현행과 함께 비교하는 설정. 2026-10-06 새 현행(XRP·SUI·LINK, 익절 환율 고정,
# 진입 필터, 시간손절·투입 상한 없음)으로 모의매매를 시작하기 전에 정했다.
# 결과를 보고 고르면 그 기간에만 맞춘 설정이 되므로, 바꾸려면 다음 기간 데이터로 다시 확인한다.
OLD_COINS = "SOL+AVAX+LINK+BCH+SUI+UNI+TAO+BSV+AAVE+ATOM"       # 2026-09-27 ~ 10-06 운영 코인
PRESETS = [
    ("시간손절 72h", "time_stop=72"),
    ("시간손절 24h", "time_stop=24"),
    ("투입 상한 50%", "cap=0.5"),
    ("진입 필터 끔", "entry_q=none"),
    ("익절 지금 환율", "tp=exit"),
    ("spacing 0.4", "spacing=0.4"),
    ("코인 + BTC·ETH", "coins=XRP+SUI+LINK+BTC+ETH"),
    ("코인 BTC·ETH·XRP·SOL", "coins=BTC+ETH+XRP+SOL"),
    ("옛 현행 (09-27~10-06)", f"tp=exit,entry_q=none,time_stop=24,coins={OLD_COINS}"),
]

_KEYS = ("tp", "entry_q", "window", "cap", "coins", "leverage", "spacing", "slots", "total", "time_stop", "grid")


class _Book:
    """전 코인이 함께 보는 장부 — 투입 상한 판단용."""
    def __init__(self, cap):
        self.cap, self.grids = cap, []

    def room(self, krw: float) -> bool:
        if self.cap is None:
            return True
        full = sum(g.cpg * g.n_slots for g in self.grids)
        used = sum(g.cpg * g.active_count() for g in self.grids)
        return used + krw <= self.cap * full + 1e-6


class VariantGrid(pt.PaperCoinGrid):
    """분석용 확장: 투입 상한(전 코인 공유 장부)과 최우선 잔량 초과 주문 집계. 전략 규칙은 PaperCoinGrid."""
    def __init__(self, coin: str, cfg: dict, book: _Book = None):
        super().__init__(coin, cfg)
        if book is None:
            book = _Book(cfg.get("cap"))
            book.grids.append(self)
        self.book = book
        self.orders = self.thin = 0                # 주문 수 / 그중 최우선 잔량보다 컸던 주문 수

    @classmethod
    def make_grids(cls, coins_cfg: dict) -> dict:
        """투입 상한은 전 코인 합계로 판단하므로 장부 하나를 함께 쓰게 만든다."""
        cap = next(iter(coins_cfg.values())).get("cap") if coins_cfg else None
        book = _Book(cap)
        grids = {c: cls(c, cfg, book) for c, cfg in coins_cfg.items()}
        book.grids = list(grids.values())
        return grids

    def quote(self, up: dict, bg: dict, usd_krw: float) -> dict:
        q = super().quote(up, bg, usd_krw)
        u, b = up[self.cfg["upbit_market"]], bg[self.cfg["bitget_symbol"]]
        q.update(up_bid_sz=u[2], up_ask_sz=u[3], bg_bid_sz=b[2], bg_ask_sz=b[3])
        return q

    def _entry_ok(self, q: dict) -> bool:
        return super()._entry_ok(q) and self.book.room(self.cpg)     # 투입 상한

    def _enter_slot(self, slot, q: dict, usd_krw: float):
        super()._enter_slot(slot, q, usd_krw)
        self.orders += 1
        self.thin += slot.coin_qty > q["up_ask_sz"] or slot.short_qty > q["bg_bid_sz"]

    def _exit_slot(self, slot, q: dict, upbit_usdt_krw: float, trade_id: int, reason: str = "익절"):
        self.orders += 1
        self.thin += slot.coin_qty > q["up_bid_sz"] or slot.short_qty > q["bg_ask_sz"]
        super()._exit_slot(slot, q, upbit_usdt_krw, trade_id, reason)


# ── 설정 문자열 → 코인별 설정 ─────────────────────────────────────────

def coin_cfg(coin: str, template: dict) -> dict:
    """모의매매 설정에 없는 코인(BTC 등)의 설정. 거래소 심볼만 바꾼다."""
    cfg = dict(template)
    cfg.update(upbit_market=f"KRW-{coin}", bitget_symbol=f"{coin}USDT", currency=coin)
    return cfg


def parse(spec: str) -> dict:
    opts = {}
    for part in [p.strip() for p in spec.split(",") if p.strip()]:
        k, v = [x.strip() for x in part.split("=", 1)]
        if k not in _KEYS:
            raise ValueError(f"모르는 설정: {k} ({', '.join(_KEYS)} 중 하나)")
        opts[k] = v
    if opts.get("tp", "entry") not in ("entry", "exit"):
        raise ValueError("tp 는 entry(진입 환율 고정) 또는 exit(현행) 중 하나")
    return opts


def build(spec: str, base_cfg: dict, total: int) -> tuple:
    """설정 문자열로 코인별 설정을 만든다. 반환: (coins_cfg, time_stop 또는 None, 그리드 클래스, 총자본)
    업비트 자본 = 총자본 × L/(L+1) ÷ 코인 수 — 모의매매 설정(paper_settings)과 같은 식이라
    빈 문자열이면 현행 설정과 같은 값이 나온다.
    그리드는 옵션이 없어도 VariantGrid 다 (현행과 똑같이 거래하면서 청산 사유·잔량 초과를 기록)."""
    opts = parse(spec)
    template = next(iter(base_cfg.values()))
    coins = [c.upper() for c in opts["coins"].split("+")] if "coins" in opts else list(base_cfg)
    cfgs = {c: dict(base_cfg[c]) if c in base_cfg else coin_cfg(c, template) for c in coins}
    lev = int(opts.get("leverage", template["leverage"]))
    tot = int(float(opts.get("total", total)))
    for cfg in cfgs.values():
        cfg["leverage"] = lev
        cfg["upbit_capital"] = tot * lev // (lev + 1) // len(cfgs)
        if "spacing" in opts:
            cfg["spacing"] = float(opts["spacing"])
        if "slots" in opts:
            cfg["n_slots"] = int(opts["slots"])
        if "tp" in opts:
            cfg["tp"] = opts["tp"]
        if "entry_q" in opts:
            cfg["entry_q"] = None if opts["entry_q"].lower() in ("none", "off") else float(opts["entry_q"])
        for k in ("window", "cap"):
            if k in opts:
                cfg[k] = float(opts[k])
    if "time_stop" not in opts:
        time_stop = None
    elif opts["time_stop"].lower() in ("none", "off"):
        time_stop = float("inf")              # (보유시간 >= inf) 는 항상 거짓 → 시간손절 안 함
    else:
        time_stop = float(opts["time_stop"])
    if "grid" in opts:
        mod, cls = opts["grid"].split(":")
        grid_cls = getattr(importlib.import_module(mod), cls)
    else:
        grid_cls = VariantGrid
    return cfgs, time_stop, grid_cls, tot


def history_hours(specs: list, base_cfg: dict = None) -> float:
    """진입 필터 창 중 가장 긴 것. 리플레이 시작 전 이만큼 기록을 미리 흘려 둔다 (현행 설정 포함)."""
    hs = [float(c.get("window", 24)) for c in (base_cfg or {}).values() if c.get("entry_q") is not None]
    for s in specs:
        o = parse(s)
        if o.get("entry_q", "").lower() not in ("none", "off"):
            hs.append(float(o.get("window", 24)))
    return max(hs) if hs else 0.0
