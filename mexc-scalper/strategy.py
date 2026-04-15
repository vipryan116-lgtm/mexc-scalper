"""信号检测 + 仓位计算.

两套策略:
  - mean_revert: 触及 ±σ 外 + CVD 转向 + Delta 翻色 + DOM 确认 -> 反弹/回落
  - breakout:    价格稳定偏离 + CVD 新高 + Delta 连续同色 + DOM 确认 -> 顺势

每个策略有 0-4 分评分, score >= min_score 才出信号.
"""
import time
from dataclasses import dataclass, field
from typing import Optional, List

import numpy as np

from config import Config


@dataclass
class Signal:
    ts: int
    kind: str                     # mean_revert_long / _short / breakout_long / _short
    side: str                     # "buy" / "sell"
    entry: float
    stop: float
    target: float
    rr: float
    qty: float                    # 合约张数
    score: int
    reasons: List[str] = field(default_factory=list)


class StrategyEngine:
    def __init__(self, cfg: Config, store):
        self.cfg = cfg
        self.store = store
        self._last_signal_ts = 0.0
        self._cooldown_until = 0.0
        self._consec_losses = 0
        self._last_signal_kind = None

    # ------------------------------------------------------------------
    # feature helpers
    # ------------------------------------------------------------------
    def _stats(self, bars):
        if not bars:
            return None
        totals = {}
        for b in bars:
            for p, (bv, sv) in b["levels"].items():
                totals[p] = totals.get(p, 0.0) + bv + sv
        if not totals:
            return None
        prices = np.array(sorted(totals.keys()), dtype=float)
        vols = np.array([totals[p] for p in prices], dtype=float)
        tv = vols.sum()
        if tv <= 0:
            return None
        mean = float((prices * vols).sum() / tv)
        var = float(((prices - mean) ** 2 * vols).sum() / tv)
        sigma = float(np.sqrt(max(var, 0.0)))
        poc = float(prices[int(vols.argmax())])
        return mean, sigma, poc

    def _cvd_slope(self, lookback: int) -> float:
        cvd = self.store.cvd
        if len(cvd) < 2:
            return 0.0
        window = list(cvd)[-lookback:]
        if len(window) < 2:
            return 0.0
        return float(window[-1][1] - window[0][1])

    def _delta_streak(self, n: int) -> int:
        """正值 = 最近 n 根里绿柱数量减红柱, 负值反之."""
        bars = list(self.store.bars["1s"])[-n:]
        if not bars:
            return 0
        return sum(1 if (b["buy"] - b["sell"]) > 0 else -1 for b in bars)

    def _dom_imbalance(self) -> float:
        s = self.store
        if not s.bids or not s.asks:
            return 1.0
        b = sum(v for _, v in s.bids[:5])
        a = sum(v for _, v in s.asks[:5])
        if a <= 0:
            return 10.0
        return b / a

    # ------------------------------------------------------------------
    # main
    # ------------------------------------------------------------------
    def evaluate(self) -> Optional[Signal]:
        now = time.time()
        if now < self._cooldown_until:
            return None
        if self._consec_losses >= self.cfg.filters.max_consecutive_losses:
            return None

        s = self.store
        if s.last_price is None or not s.bars["1s"]:
            return None

        bars = list(s.bars["1s"])
        stats = self._stats(bars)
        if stats is None:
            return None
        mean, sigma, poc = stats
        if sigma <= 0:
            return None

        price = s.last_price
        z = (price - mean) / sigma

        sig = None
        if self.cfg.mean_revert.enabled:
            sig = self._mean_revert(price, mean, sigma, poc, z)
        if sig is None and self.cfg.breakout.enabled:
            sig = self._breakout(price, mean, sigma, poc, z)
        if sig is None:
            return None

        if now - self._last_signal_ts < self.cfg.filters.cooldown_sec:
            return None
        self._last_signal_ts = now
        self._last_signal_kind = sig.kind
        return sig

    # ------------------------------------------------------------------
    def _mean_revert(self, price, mean, sigma, poc, z) -> Optional[Signal]:
        cfg = self.cfg.mean_revert

        if z <= -cfg.sigma_trigger:
            reasons = [f"价格 {z:+.2f}σ (超卖)"]
            score = 1
            if self._cvd_slope(30) >= 0:
                reasons.append("CVD 转正"); score += 1
            if self._delta_streak(3) >= 0:
                reasons.append("Delta 转绿"); score += 1
            dom = self._dom_imbalance()
            if dom >= self.cfg.filters.min_dom_imbalance:
                reasons.append(f"DOM 买墙厚 {dom:.2f}"); score += 1
            if score < cfg.min_score:
                return None
            stop = mean - sigma * (cfg.sigma_trigger + cfg.stop_sigma_buffer)
            target = mean if cfg.target == "vwap" else poc
            return self._build("mean_revert_long", "buy", price, stop, target,
                               score, reasons)

        if z >= cfg.sigma_trigger:
            reasons = [f"价格 {z:+.2f}σ (超买)"]
            score = 1
            if self._cvd_slope(30) <= 0:
                reasons.append("CVD 转负"); score += 1
            if self._delta_streak(3) <= 0:
                reasons.append("Delta 转红"); score += 1
            dom = self._dom_imbalance()
            if dom <= 1.0 / self.cfg.filters.min_dom_imbalance:
                reasons.append(f"DOM 卖墙厚 {dom:.2f}"); score += 1
            if score < cfg.min_score:
                return None
            stop = mean + sigma * (cfg.sigma_trigger + cfg.stop_sigma_buffer)
            target = mean if cfg.target == "vwap" else poc
            return self._build("mean_revert_short", "sell", price, stop, target,
                               score, reasons)
        return None

    # ------------------------------------------------------------------
    def _breakout(self, price, mean, sigma, poc, z) -> Optional[Signal]:
        cfg = self.cfg.breakout

        if cfg.sigma_min <= z < self.cfg.mean_revert.sigma_trigger:
            reasons = [f"价格 {z:+.2f}σ (趋势向上)"]
            score = 1
            if self._cvd_slope(cfg.cvd_lookback) > 0:
                reasons.append("CVD 新高"); score += 1
            if self._delta_streak(cfg.delta_streak) >= cfg.delta_streak:
                reasons.append(f"Delta {cfg.delta_streak} 连绿"); score += 1
            dom = self._dom_imbalance()
            if dom >= self.cfg.filters.min_dom_imbalance:
                reasons.append(f"DOM 买墙厚 {dom:.2f}"); score += 1
            if score < cfg.min_score:
                return None
            if cfg.stop_method == "poc":
                stop = poc - sigma * 0.3
            else:
                bars = list(self.store.bars["1s"])[-5:]
                stop = min(b["l"] for b in bars) if bars else price - sigma
            stop_dist = price - stop
            if stop_dist <= 0:
                return None
            target = price + stop_dist * self.cfg.risk.target_rr
            return self._build("breakout_long", "buy", price, stop, target,
                               score, reasons)

        if -self.cfg.mean_revert.sigma_trigger < z <= -cfg.sigma_min:
            reasons = [f"价格 {z:+.2f}σ (趋势向下)"]
            score = 1
            if self._cvd_slope(cfg.cvd_lookback) < 0:
                reasons.append("CVD 新低"); score += 1
            if self._delta_streak(cfg.delta_streak) <= -cfg.delta_streak:
                reasons.append(f"Delta {cfg.delta_streak} 连红"); score += 1
            dom = self._dom_imbalance()
            if dom <= 1.0 / self.cfg.filters.min_dom_imbalance:
                reasons.append(f"DOM 卖墙厚 {dom:.2f}"); score += 1
            if score < cfg.min_score:
                return None
            if cfg.stop_method == "poc":
                stop = poc + sigma * 0.3
            else:
                bars = list(self.store.bars["1s"])[-5:]
                stop = max(b["h"] for b in bars) if bars else price + sigma
            stop_dist = stop - price
            if stop_dist <= 0:
                return None
            target = price - stop_dist * self.cfg.risk.target_rr
            return self._build("breakout_short", "sell", price, stop, target,
                               score, reasons)
        return None

    # ------------------------------------------------------------------
    def _build(self, kind, side, entry, stop, target, score, reasons) -> Optional[Signal]:
        stop_dist = abs(entry - stop)
        tgt_dist = abs(target - entry)
        if stop_dist <= 0 or tgt_dist <= 0:
            return None
        rr = tgt_dist / stop_dist
        if rr < self.cfg.risk.min_rr:
            return None

        # 仓位: risk_usdt / (止损距离 * 合约乘数)
        risk_usdt = (
            self.cfg.account.equity_usdt
            * self.cfg.risk.max_risk_per_trade_pct
            / 100.0
        )
        mult = max(self.cfg.trading.contract_multiplier, 1e-12)
        qty = risk_usdt / (stop_dist * mult)
        qty = max(1.0, round(qty))

        return Signal(
            ts=int(time.time() * 1000),
            kind=kind, side=side,
            entry=entry, stop=stop, target=target,
            rr=rr, qty=qty,
            score=score, reasons=reasons,
        )

    # ------------------------------------------------------------------
    def register_result(self, pnl: float):
        """成交结算后调用, 更新连亏计数 / 冷静期."""
        if pnl < 0:
            self._consec_losses += 1
            self._cooldown_until = time.time() + 300
        else:
            self._consec_losses = 0
