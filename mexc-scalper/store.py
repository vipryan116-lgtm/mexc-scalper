"""数据存储 + 多周期聚合. UI 层订阅 big_trade 信号, 其它数据靠定时器拉取."""
from collections import deque, defaultdict
from PyQt6.QtCore import QObject, pyqtSignal

INTERVALS = {"1s": 1, "1m": 60, "5m": 300}
MAXLEN = {"1s": 300, "1m": 240, "5m": 288}  # ≈5 分 / 4 小时 / 24 小时


class DataStore(QObject):
    big_trade = pyqtSignal(dict)

    def __init__(self, symbol: str, big_trade_vol: float):
        super().__init__()
        self.symbol = symbol
        self.big_trade_vol = big_trade_vol
        self.trades = deque(maxlen=3000)
        self.bars = {k: deque(maxlen=MAXLEN[k]) for k in INTERVALS}
        self.cvd = deque(maxlen=3000)
        self._cvd_val = 0.0
        self.bids = []
        self.asks = []
        self.last_price = None

    def on_trade(self, d: dict):
        try:
            price = float(d["p"])
            vol = float(d["v"])
            side = int(d["T"])   # 1=主动买, 2=主动卖
            ts = int(d["t"])
        except (KeyError, TypeError, ValueError):
            return

        self.last_price = price
        signed = vol if side == 1 else -vol
        self._cvd_val += signed
        self.cvd.append((ts, self._cvd_val))
        self.trades.append({"ts": ts, "p": price, "v": vol, "side": side})

        for name, interval in INTERVALS.items():
            self._update_bar(self.bars[name], interval, price, vol, side, ts)

        if vol >= self.big_trade_vol:
            self.big_trade.emit({"ts": ts, "p": price, "v": vol, "side": side})

    def _update_bar(self, bars, interval, price, vol, side, ts_ms):
        bts = (ts_ms // 1000 // interval) * interval
        if not bars or bars[-1]["ts"] != bts:
            bars.append({
                "ts": bts,
                "o": price, "h": price, "l": price, "c": price,
                "buy": 0.0, "sell": 0.0,
                "vwap_num": 0.0, "vwap_den": 0.0,
                "levels": defaultdict(lambda: [0.0, 0.0]),
            })
        bar = bars[-1]
        if price > bar["h"]:
            bar["h"] = price
        if price < bar["l"]:
            bar["l"] = price
        bar["c"] = price
        if side == 1:
            bar["buy"] += vol
        else:
            bar["sell"] += vol
        bar["vwap_num"] += price * vol
        bar["vwap_den"] += vol
        bar["levels"][round(price, 4)][0 if side == 1 else 1] += vol

    def on_depth(self, d: dict):
        try:
            self.asks = [(float(a[0]), float(a[1])) for a in (d.get("asks") or [])]
            self.bids = [(float(b[0]), float(b[1])) for b in (d.get("bids") or [])]
        except Exception:
            pass
