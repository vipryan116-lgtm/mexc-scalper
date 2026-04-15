"""MEXC 合约剥头皮面板入口.

用法:
    python main.py                           # 默认 BTC_USDT
    python main.py ETH_USDT                  # 指定交易对
    python main.py ETH_USDT 200              # 指定大单阈值 (合约张数)

策略 / 风险 / 下单参数全部在 config.yaml, 改完重启生效.
API key 放 .env (MEXC_API_KEY / MEXC_API_SECRET), 只用于撤单/查仓.
"""
import sys
import asyncio
import logging
from pathlib import Path

from dotenv import load_dotenv
import qasync
from PyQt6.QtWidgets import QApplication

from config import Config
from store import DataStore
from ui import Dashboard
from mexc_ws import MexcContractWS
from strategy import StrategyEngine
from bridge import ExtensionBridge, start_bridge_server
from exchange import MexcCCXT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-10s  %(message)s",
    datefmt="%H:%M:%S",
)


def main():
    root = Path(__file__).parent
    load_dotenv(root / ".env")

    cfg = Config.load(root / "config.yaml")

    symbol = sys.argv[1] if len(sys.argv) > 1 else cfg.trading.symbol
    big_trade_vol = float(sys.argv[2]) if len(sys.argv) > 2 else 50.0
    cfg.trading.symbol = symbol

    app = QApplication(sys.argv)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    store = DataStore(symbol, big_trade_vol)
    strategy = StrategyEngine(cfg, store)
    bridge = ExtensionBridge()
    exchange = MexcCCXT(symbol)
    exchange.connect()   # 失败则 ready=False, Live 反手会被拒

    dash = Dashboard(
        store, config=cfg, strategy=strategy,
        bridge=bridge, exchange=exchange,
    )
    dash.show()

    ws = MexcContractWS(symbol, store)
    loop.create_task(ws.run())
    loop.create_task(start_bridge_server(bridge, port=cfg.trading.bridge_port))

    print(f"[main] symbol={symbol}  bridge_port={cfg.trading.bridge_port}  "
          f"ccxt={'on' if exchange.ready else 'off'}")
    print(f"[main] 浏览器打开 MEXC 永续 {symbol} 页面, 加载扩展, 切到只挂/单向持仓")
    print("[main] 等待行情 + 扩展 WS 连入...")

    with loop:
        loop.run_forever()


if __name__ == "__main__":
    main()
