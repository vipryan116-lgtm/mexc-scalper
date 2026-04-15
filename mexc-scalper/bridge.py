"""最小化 MEXC Chrome 扩展 WS 宿主.

原始实现: delta_neutral_lp_mexc/engine/adapters/extension_bridge.py
这里剥到最小: 只保留 place_limit_order / place_market_order / close_position / ping.
宿主 FastAPI 跑在 127.0.0.1:8080 (扩展硬编码), 与 delta_neutral 引擎端口冲突.
"""
import asyncio
import json
import logging
import uuid
from typing import Optional, Dict

logger = logging.getLogger("bridge")


class ExtensionBridge:
    def __init__(self, timeout: float = 15.0):
        self._ws = None
        self._timeout = timeout
        self._pending: Dict[str, asyncio.Future] = {}
        self._connected = False
        self._order_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._connected and self._ws is not None

    async def handle_connection(self, ws):
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._cancel_pending("replaced")
        await ws.accept()
        self._ws = ws
        self._connected = True
        logger.info("扩展已连接")
        try:
            while True:
                raw = await ws.receive_text()
                self._on_msg(raw)
        except Exception as e:
            logger.info(f"扩展断开: {e}")
        finally:
            self._ws = None
            self._connected = False
            self._cancel_pending("disconnected")

    def _on_msg(self, raw: str):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        rid = msg.get("id")
        if rid and rid in self._pending:
            fut = self._pending.pop(rid)
            if not fut.done():
                fut.set_result(msg)

    def _cancel_pending(self, reason: str):
        for rid, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_result({"id": rid, "success": False, "error": reason})
        self._pending.clear()

    # ------------------------------------------------------------------
    async def _send(self, action: str, params: Optional[dict] = None) -> dict:
        if not self.connected:
            return {"success": False, "error": "扩展未连接"}
        if action == "place_order":
            async with self._order_lock:
                return await self._do_send(action, params)
        return await self._do_send(action, params)

    async def _do_send(self, action: str, params: Optional[dict]) -> dict:
        rid = f"req_{uuid.uuid4().hex[:8]}"
        msg = {"id": rid, "action": action}
        if params:
            msg["params"] = params
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self._pending[rid] = fut
        try:
            await self._ws.send_json(msg)
            return await asyncio.wait_for(fut, timeout=self._timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            return {"success": False, "error": "timeout"}
        except Exception as e:
            self._pending.pop(rid, None)
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    async def place_limit_order(self, side: str, amount: float,
                                price: float) -> dict:
        logger.info(f"[EXT] 限价 {side} {amount} @ {price}")
        return await self._send("place_order", {
            "type": "limit", "side": side,
            "amount": amount, "price": price,
        })

    async def place_market_order(self, side: str, amount: float) -> dict:
        logger.info(f"[EXT] 市价 {side} {amount}")
        return await self._send("place_order", {
            "type": "market", "side": side, "amount": amount,
        })

    async def close_position(self, side: str, amount: float) -> dict:
        """side: closeLong / closeShort"""
        return await self._send("place_order", {
            "type": "close", "side": side, "amount": amount,
        })

    async def place_trigger_order(self, side: str, amount: float,
                                  trigger_price: float, price: float) -> dict:
        """条件限价单. 触发后以 price 限价成交.
        用法: 开仓后反向挂 SL/TP; 要求账户是单向持仓 (one-way)."""
        logger.info(
            f"[EXT] 触发 {side} {amount} trig@{trigger_price} lim@{price}"
        )
        return await self._send("place_order", {
            "type": "trigger", "side": side, "amount": amount,
            "triggerPrice": trigger_price, "price": price,
        })

    async def ping(self) -> bool:
        r = await self._send("ping")
        return bool(r.get("success", False))


# ---------------------------------------------------------------------------
def create_app(bridge: ExtensionBridge):
    from fastapi import FastAPI, WebSocket
    app = FastAPI()

    @app.websocket("/ws/extension")
    async def _ws(websocket: WebSocket):
        await bridge.handle_connection(websocket)

    @app.get("/health")
    def _health():
        return {"connected": bridge.connected}

    return app


async def start_bridge_server(bridge: ExtensionBridge, port: int = 8080):
    """在当前事件循环里启动 uvicorn 宿主. 返回 Server 实例."""
    import uvicorn
    app = create_app(bridge)
    cfg = uvicorn.Config(
        app, host="127.0.0.1", port=port,
        log_level="warning", access_log=False,
    )
    server = uvicorn.Server(cfg)
    asyncio.create_task(server.serve())
    return server
