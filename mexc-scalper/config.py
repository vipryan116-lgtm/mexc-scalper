"""config.yaml 加载 + dataclass 结构."""
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class AccountCfg:
    equity_usdt: float = 1000.0


@dataclass
class RiskCfg:
    max_risk_per_trade_pct: float = 0.5
    min_rr: float = 1.5
    target_rr: float = 2.0


@dataclass
class MeanRevertCfg:
    enabled: bool = True
    sigma_trigger: float = 1.8
    stop_sigma_buffer: float = 0.3
    target: str = "vwap"
    min_score: int = 3


@dataclass
class BreakoutCfg:
    enabled: bool = True
    sigma_min: float = 0.3
    cvd_lookback: int = 60
    delta_streak: int = 3
    stop_method: str = "poc"
    min_score: int = 3


@dataclass
class FiltersCfg:
    min_dom_imbalance: float = 1.3
    cooldown_sec: int = 30
    max_consecutive_losses: int = 2


@dataclass
class TradingCfg:
    symbol: str = "BTC_USDT"
    contract_multiplier: float = 0.0001
    mode: str = "paper"
    bridge_port: int = 8080
    default_live: bool = False


@dataclass
class Config:
    account: AccountCfg = field(default_factory=AccountCfg)
    risk: RiskCfg = field(default_factory=RiskCfg)
    mean_revert: MeanRevertCfg = field(default_factory=MeanRevertCfg)
    breakout: BreakoutCfg = field(default_factory=BreakoutCfg)
    filters: FiltersCfg = field(default_factory=FiltersCfg)
    trading: TradingCfg = field(default_factory=TradingCfg)

    @classmethod
    def load(cls, path):
        p = Path(path)
        if not p.exists():
            return cls()
        with open(p, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return cls(
            account=AccountCfg(**(raw.get("account") or {})),
            risk=RiskCfg(**(raw.get("risk") or {})),
            mean_revert=MeanRevertCfg(**(raw.get("mean_revert") or {})),
            breakout=BreakoutCfg(**(raw.get("breakout") or {})),
            filters=FiltersCfg(**(raw.get("filters") or {})),
            trading=TradingCfg(**(raw.get("trading") or {})),
        )
