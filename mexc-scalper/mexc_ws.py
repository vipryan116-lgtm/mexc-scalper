"""MEXC 合约公共行情 WebSocket 客户端.

订阅:
  - sub.deal        逐笔成交 (含 taker 方向 T: 1=买, 2=卖)
  - sub.depth.full  20 档完整盘口 (约 100ms 推送一次)
"""
import asyncio
import json
import websockets

WS_URL = "wss://contract.mexc.com/edge"


class MexcContractWS:
    def __init__(self, symbol: str, store):
        self.symbol = symbol
        self.store = store

    async def run(self):
        while True:
            try:
                async with websockets.connect(
                    WS_URL, ping_interval=None, max_size=2**22
                ) as ws:
                    await self._subscribe(ws)
                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    try:
                        async for raw in ws:
                            try:
                                self._handle(json.loads(raw))
                            except Exception as e:
                                print(f"[ws] handle error: {e}")
                    finally:
                        ping_task.cancel()
            except Exception as e:
                print(f"[ws] disconnected ({e}); reconnecting in 3s")
                await asyncio.sleep(3)

    async def _subscribe(self, ws):
        subs = [
            {"method": "sub.deal", "param": {"symbol": self.symbol}},
            {"method": "sub.depth.full",
             "param": {"symbol": self.symbol, "limit": 20}},
        ]
        for s in subs:
            await ws.send(json.dumps(s))
        print(f"[ws] subscribed {self.symbol}")

    async def _ping_loop(self, ws):
        try:
            while True:
                await asyncio.sleep(15)
                await ws.send(json.dumps({"method": "ping"}))
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    def _handle(self, msg):
        ch = msg.get("channel", "")
        data = msg.get("data")
        if ch == "push.deal":
            if isinstance(data, list):
                for d in data:
                    self.store.on_trade(d)
            elif isinstance(data, dict):
                self.store.on_trade(data)
        elif ch == "push.depth.full":
            if isinstance(data, dict):
                self.store.on_depth(data)
        elif ch == "pong":
            pass
