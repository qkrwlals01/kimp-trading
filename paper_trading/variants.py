"""
개선안 — 현행 전략과 같은 호가로 비교할 규칙들
────────────────────────────────────────────────────────────────────
서버에서 도는 모의매매 코드(paper_trader.PaperCoinGrid)는 그대로 두고, 상속해서 규칙만 바꾼다.
옵션을 모두 끄면 현행과 똑같이 거래해야 한다. 판정 리포트가 매번 현행 재생과 대조해 확인한다.

설정 문자열 (쉼표로 여러 개, 예: "tp=entry,entry_q=0.2")
  tp=entry       ① 익절 판단을 진입 때 은행 환율로 고정한다.
                   현행은 청산 김프를 지금 환율로 다시 계산해 목표와 비교한다. 그러면 가격은
                   그대로인데 환율만 내려도 익절 신호가 난다 (9/27~30 익절 235건 중 74건이 수수료 후 적자).
                   진입 환율로 고정한 김프 변화 (1+k₀)(a−b)/b 는 실제 손익 (a−1)−γ(b−1) 과
                   2차 항만큼만 다르다 (a·b: 업비트·비트겟 가격 변화율, γ: USDT 환산 비율, k₀: 진입 김프).
  coins=A+B+C    ② 운영 코인 교체. 총자본은 그대로 두고 코인 수로 나눈다 (코인이 적으면 슬롯이 커진다).
  entry_q=0.2    ③ 진입 필터: 진입김프가 최근 window 시간 분포의 하위 q 이하일 때만 진입.
  window=24        진입 필터 창(시간). 창의 25% 이상 쌓여야 판단한다. 리플레이는 시작 전 기록으로 미리 채운다.
  cap=0.5        ④ 투입 상한: 전 코인 열린 슬롯 금액 합계 ≤ 업비트 자본 × cap.
                   열린 슬롯 금액만큼 원/달러 환율에 노출되므로 환율 노출 상한이기도 하다.
  leverage=3     레버리지. 업비트:비트겟 자본 배분(L:1)과 가상손절 거리(5배 +16%, 3배 +27%)가 바뀐다.
  spacing=0.4  slots=5  total=10000000  time_stop=72    현행 설정 덮어쓰기
  grid=모듈:클래스   직접 만든 PaperCoinGrid 상속 클래스

실행:
      python -m paper_trading.replay --start 2026-09-27T07:00:59Z --variant "tp=entry,entry_q=0.2"
      python tools/weekly_report.py         # 아래 PRESETS 를 현행과 함께 비교
"""

import bisect, importlib
from collections import deque

from paper_trading import paper_trader as pt

# 판정 리포트가 기본으로 비교하는 개선안. 값은 판정 주간 결과를 보기 전에 정했다.
# 결과를 보고 값을 고르면 그 주에만 맞춘 설정이 되므로, 바꾸려면 다음 주 데이터로 다시 확인한다.
#   코인: 모의매매 시작 전 하루(9/26~27) 즉시왕복 호가비용 0.07% 이하 — 스프레드는 거의 안 변하는
#         구조적 성질이라 판정 주간 손익으로 고르는 것보다 덜 과적합된다 (시작 후에도 순위 동일)
#   진입 필터: coin_selector 기본값 (24h 하위 20%)   상한: 업비트 자본의 절반
TIGHT_COINS = "BTC+ETH+XRP+SOL"
_ALL = f"tp=entry,coins={TIGHT_COINS},entry_q=0.2,cap=0.5"
PRESETS = [
    ("① 익절 환율 고정", "tp=entry"),
    ("② 코인 교체 4개", f"coins={TIGHT_COINS}"),
    ("③ 진입 필터 하위20%", "entry_q=0.2"),
    ("④ 투입 상한 50%", "cap=0.5"),
    ("①+②", f"tp=entry,coins={TIGHT_COINS}"),
    ("①+②+③", f"tp=entry,coins={TIGHT_COINS},entry_q=0.2"),
    ("①~④", _ALL),
    ("①~④ spacing 0.4", _ALL + ",spacing=0.4"),
    ("①~④ 시간손절 72h", _ALL + ",time_stop=72"),
    ("①~④ 레버리지 3배", _ALL + ",leverage=3"),
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
    def __init__(self, coin: str, cfg: dict, book: _Book = None):
        super().__init__(coin, cfg)
        self.tp_entry_fx = cfg.get("tp", "exit") == "entry"
        self.entry_q = cfg.get("entry_q")
        self.window_s = cfg.get("window", 24) * 3600
        if book is None:
            book = _Book(cfg.get("cap"))
            book.grids.append(self)
        self.book = book
        self._hist, self._sorted = deque(), []     # 진입 필터용 최근 진입김프 (시각순 / 크기순)
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
        q.update(fx=usd_krw, up_bid_sz=u[2], up_ask_sz=u[3], bg_bid_sz=b[2], bg_ask_sz=b[3])
        return q

    # ── ③ 진입 필터 ──
    def _observe(self, t: float, prem: float):
        self._hist.append((t, prem))
        bisect.insort(self._sorted, prem)
        while self._hist[0][0] < t - self.window_s:
            _, old = self._hist.popleft()
            del self._sorted[bisect.bisect_left(self._sorted, old)]

    def warm(self, up: dict, bg: dict, usd_krw: float):
        """리플레이 시작 전 기록으로 진입 필터 분포만 채운다 (거래는 하지 않음)."""
        if self.entry_q is None:
            return
        try:
            q = pt.PaperCoinGrid.quote(self, up, bg, usd_krw)
        except ValueError:
            return
        self._observe(pt.time.time(), q["entry_pct"])

    def _entry_ok(self, q: dict) -> bool:
        if self.entry_q is not None:
            if self._hist[-1][0] - self._hist[0][0] < self.window_s * 0.25:
                return False
            if q["entry_pct"] > self._sorted[int(self.entry_q * (len(self._sorted) - 1))]:
                return False
        return self.book.room(self.cpg)             # ④ 투입 상한

    # ── ① 익절 판단 ──
    def _tp_kimp(self, slot, q: dict) -> float:
        if self.tp_entry_fx:
            return pt._kimp(q["up_bid"], q["bg_ask"], slot.entry_usd_krw)
        return q["exit_pct"]

    def _enter_slot(self, slot, q: dict, usd_krw: float):
        super()._enter_slot(slot, q, usd_krw)
        self.orders += 1
        self.thin += slot.coin_qty > q["up_ask_sz"] or slot.short_qty > q["bg_bid_sz"]

    def _exit_slot(self, slot, q: dict, upbit_usdt_krw: float, trade_id: int, reason: str = "익절"):
        """거래 기록에 청산 사유와 청산 때 은행 환율을 붙인다.
        ① 에서는 익절이어도 지금 환율 청산김프가 목표보다 낮을 수 있어, 김프로는 사유를 알 수 없다."""
        self.orders += 1
        self.thin += slot.coin_qty > q["up_bid_sz"] or slot.short_qty > q["bg_ask_sz"]
        append = pt._append_trade

        def tagged(row):
            row.update(reason=reason, exit_fx=q["fx"])
            append(row)

        pt._append_trade = tagged
        try:
            super()._exit_slot(slot, q, upbit_usdt_krw, trade_id, reason)
        finally:
            pt._append_trade = append

    def tick(self, up: dict, bg: dict, usd_krw: float, upbit_usdt_krw: float,
             trade_counter_ref: list):
        """PaperCoinGrid.tick 과 같은 순서 (가상손절 → 시간손절 → 익절 → 진입). 바뀐 곳만 표시."""
        try:
            q = self.quote(up, bg, usd_krw)
            self._accrue_funding(q)
            now = pt.time.time()
            if self.entry_q is not None:
                self._observe(now, q["entry_pct"])                          # ③

            self._check_stoploss(q, upbit_usdt_krw, trade_counter_ref)

            for slot in list(self.slots):
                if slot.active and (now - slot.entry_time) / 3600 >= pt.TIME_STOP_HOURS:
                    trade_counter_ref[0] += 1
                    self._exit_slot(slot, q, upbit_usdt_krw, trade_counter_ref[0], reason="시간손절")

            for slot in self.slots:
                if slot.active and self._tp_kimp(slot, q) >= slot.target_premium:   # ①
                    trade_counter_ref[0] += 1
                    self._exit_slot(slot, q, upbit_usdt_krw, trade_counter_ref[0], reason="익절")

            for slot in self.slots:
                if not slot.active and self.active_count() < self.n_slots:
                    if self._can_enter(q["entry_pct"]) and self._entry_ok(q):         # ③ ④
                        self._enter_slot(slot, q, usd_krw)

        except Exception as e:
            pt.logger.error(f"  [개선안/{self.coin}] 오류: {e}", exc_info=True)


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
        for k in ("entry_q", "window", "cap"):
            if k in opts:
                cfg[k] = float(opts[k])
    time_stop = float(opts["time_stop"]) if "time_stop" in opts else None
    if "grid" in opts:
        mod, cls = opts["grid"].split(":")
        grid_cls = getattr(importlib.import_module(mod), cls)
    else:
        grid_cls = VariantGrid
    return cfgs, time_stop, grid_cls, tot


def history_hours(specs: list) -> float:
    """진입 필터 창 중 가장 긴 것. 리플레이 시작 전 이만큼 기록을 미리 흘려 둔다."""
    hs = [float(parse(s).get("window", 24)) for s in specs if "entry_q" in parse(s)]
    return max(hs) if hs else 0.0
