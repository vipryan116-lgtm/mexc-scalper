"""ccxt MEXC 合约客户端 — 仅用于撤单 / 查单 / 查仓.

下单不走这里 (会收 taker 手续费), 全部走 bridge.py 的 DOM 通道.
这个模块存在的唯一原因: 提供 "撤销孤儿 SL/TP trigger" 能力, 让 Live 模式反手变安全.

需要环境变量 MEXC_API_KEY / MEXC_API_SECRET, 由 main.py 的 dotenv 加载.
"""
import os
import logging
from typing import List, Optional

logger = logging.getLogger("exchange")


class MexcCCXT:
    def __init__(self, symbol: str):
        self.symbol = symbol   # "BTC_USDT"
        self._ex = None
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    def _ccxt_symbol(self) -> str:
        """BTC_USDT -> BTC/USDT:USDT (ccxt 统一 swap 符号)."""
        base, quote = self.symbol.split("_")
        return f"{base}/{quote}:{quote}"

    def _mexc_native(self) -> str:
        """MEXC 原生合约 API 用下划线形式."""
        return self.symbol

    def connect(self) -> bool:
        key = os.environ.get("MEXC_API_KEY", "").strip()
        sec = os.environ.get("MEXC_API_SECRET", "").strip()
        if not key or not sec:
            logger.warning("MEXC_API_KEY/SECRET 未设置, ccxt 通道不可用")
            return False
        try:
            import ccxt.async_support as ccxt
            self._ex = ccxt.mexc({
                "apiKey": key,
                "secret": sec,
                "options": {"defaultType": "swap"},
                "enableRateLimit": True,
            })
            self._ready = True
            logger.info(f"ccxt 通道就绪 ({self.symbol})")
            return True
        except ImportError:
            logger.error("未安装 ccxt, 跳过. pip install ccxt")
            return False
        except Exception as e:
            logger.error(f"ccxt 初始化失败: {e}")
            return False

    async def close(self):
        if self._ex is not None:
            try:
                await self._ex.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    async def cancel_all_orders(self) -> int:
        """取消本合约的所有常规挂单 + 触发计划单. 返回取消总数."""
        if not self._ready:
            return 0
        total = 0

        # 1) 常规限价挂单
        try:
            orders = await self._ex.fetch_open_orders(self._ccxt_symbol())
            for o in orders:
                try:
                    await self._ex.cancel_order(o["id"], self._ccxt_symbol())
                    total += 1
                except Exception as e:
                    logger.warning(f"cancel order {o.get('id')} 失败: {e}")
        except Exception as e:
            logger.warning(f"fetch_open_orders 失败: {e}")

        # 2) 计划委托 (trigger / stop / tp)
        try:
            trig_ids = await self.get_trigger_order_ids()
            for tid in trig_ids or []:
                try:
                    r = await self._ex.contract_private_post_planorder_cancel([{
                        "symbol": self._mexc_native(),
                        "orderId": int(tid),
                    }])
                    if r.get("success"):
                        total += 1
                    else:
                        logger.warning(f"planorder cancel {tid}: {r}")
                except Exception as e:
                    logger.warning(f"cancel trigger {tid} 失败: {e}")
        except Exception as e:
            logger.warning(f"trigger cancel loop 失败: {e}")

        return total

    async def get_open_orders(self) -> List[dict]:
        if not self._ready:
            return []
        try:
            orders = await self._ex.fetch_open_orders(self._ccxt_symbol())
            return orders or []
        except Exception as e:
            logger.warning(f"fetch_open_orders 失败: {e}")
            return []

    async def get_trigger_order_ids(self) -> Optional[List[str]]:
        """查询活跃的计划委托 ID. None = 查询失败."""
        if not self._ready:
            return None
        try:
            r = await self._ex.contract_private_get_planorder_list_orders({
                "symbol": self._mexc_native(),
                "states": "1",        # 1 = 活跃
                "page_num": 1,
                "page_size": 50,
            })
            if not r.get("success"):
                return None
            return [
                str(o["id"]) for o in (r.get("data") or [])
                if int(o.get("state", 0)) == 1
            ]
        except Exception as e:
            logger.warning(f"planorder list 失败: {e}")
            return None

    async def get_position(self) -> Optional[dict]:
        """查询当前持仓. 返回 None 表示空仓."""
        if not self._ready:
            return None
        try:
            positions = await self._ex.fetch_positions([self._ccxt_symbol()])
            for p in positions or []:
                sz = float(p.get("contracts") or 0)
                if sz > 0:
                    return {
                        "side": p.get("side"),
                        "qty": sz,
                        "entry": float(p.get("entryPrice") or 0),
                    }
            return None
        except Exception as e:
            logger.warning(f"fetch_positions 失败: {e}")
            return None
