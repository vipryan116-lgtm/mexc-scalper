"""剥头皮可视化面板 (PyQt6 + pyqtgraph).

布局:
  ┌── 左 (Tab) ─────────────────────┬── 右 ──────────────┐
  │ [Scalp 1s] [1m] [5m]            │ Distribution       │
  │                                 │ (正态分布 + ±σ)    │
  │  Price + VWAP + POC             ├────────────────────┤
  │  (1s 视图叠加 Bubbles 泡泡单)   │ DOM 20 档          │
  │  Footprint Delta                │                    │
  │  CVD                            ├────────────────────┤
  │                                 │ 大单 Tape          │
  └─────────────────────────────────┴────────────────────┘
"""
import time
import json
import asyncio
import threading
from pathlib import Path
from collections import deque, defaultdict

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer, QRectF, QPointF, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPicture, QPen, QBrush
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QSplitter,
    QTextEdit, QTabWidget, QLabel, QPushButton, QCheckBox, QFrame,
    QComboBox
)

pg.setConfigOption("background", "#0e0e12")
pg.setConfigOption("foreground", "#d0d0d0")
pg.setConfigOptions(antialias=False)

GREEN = "#26a69a"
RED = "#ef5350"
YELLOW = "#ffcc33"
BLUE = "#42a5f5"
GREY = "#888888"
ORANGE = "#ffaa33"


# ---------------------------------------------------------------------------
class CandlestickItem(pg.GraphicsObject):
    def __init__(self):
        super().__init__()
        self._picture = QPicture()
        self._rect = QRectF()

    def set_data(self, data):
        self._picture = QPicture()
        if not data:
            self._rect = QRectF()
            self.update()
            return
        p = QPainter(self._picture)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        body_w = 0.35
        xs, ys = [], []
        for (t, o, h, l, c) in data:
            color = QColor(GREEN) if c >= o else QColor(RED)
            p.setPen(QPen(color, 1))
            p.setBrush(QBrush(color))
            p.drawLine(QPointF(t, l), QPointF(t, h))
            top = max(o, c)
            bot = min(o, c)
            p.drawRect(QRectF(t - body_w, bot, body_w * 2, max(top - bot, 1e-9)))
            xs.append(t)
            ys.extend([h, l])
        p.end()
        self._rect = QRectF(
            min(xs) - 1, min(ys), max(xs) - min(xs) + 2, max(ys) - min(ys)
        )
        self.informViewBoundsChanged()
        self.update()

    def paint(self, painter, *_):
        painter.drawPicture(0, 0, self._picture)

    def boundingRect(self):
        return self._rect


# ---------------------------------------------------------------------------
class TimeframePanel(QWidget):
    """Candles + Delta + CVD, 可选 bubble overlay. 可复用于任意周期."""

    def __init__(self, label: str, interval_sec: int, show_bubbles: bool = False):
        super().__init__()
        self.label = label
        self.interval_sec = interval_sec
        self.show_bubbles = show_bubbles

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Orientation.Vertical)

        title_suffix = "  + Bubbles" if show_bubbles else ""
        self.px_plot = pg.PlotWidget(
            title=f"{label}  Price + VWAP + POC{title_suffix}"
        )
        self.px_plot.showGrid(x=True, y=True, alpha=0.15)
        self.candles = CandlestickItem()
        self.px_plot.addItem(self.candles)
        self.vwap_curve = self.px_plot.plot(pen=pg.mkPen(YELLOW, width=1))
        self.poc_line = pg.InfiniteLine(
            angle=0, pen=pg.mkPen(GREY, width=1, style=Qt.PenStyle.DashLine)
        )
        self.px_plot.addItem(self.poc_line)
        self.bubbles = pg.ScatterPlotItem(pxMode=True, pen=None)
        self.px_plot.addItem(self.bubbles)
        splitter.addWidget(self.px_plot)

        self.delta_plot = pg.PlotWidget(title=f"{label}  Footprint Delta")
        self.delta_plot.showGrid(x=True, y=True, alpha=0.15)
        self.delta_plot.setXLink(self.px_plot)
        self.delta_plot.addLine(y=0, pen=pg.mkPen(GREY, width=1))
        self.delta_bars = pg.BarGraphItem(x=[0], height=[0], width=0.7)
        self.delta_plot.addItem(self.delta_bars)
        splitter.addWidget(self.delta_plot)

        self.cvd_plot = pg.PlotWidget(title=f"{label}  CVD (cumulative Δ)")
        self.cvd_plot.showGrid(x=True, y=True, alpha=0.15)
        self.cvd_plot.setXLink(self.px_plot)
        self.cvd_curve = self.cvd_plot.plot(pen=pg.mkPen(BLUE, width=2))
        splitter.addWidget(self.cvd_plot)

        splitter.setSizes([460, 220, 220])
        root.addWidget(splitter)

    def update_panel(self, bars, trades=None):
        n = len(bars)
        if n == 0:
            return
        ohlc = [(i, b["o"], b["h"], b["l"], b["c"]) for i, b in enumerate(bars)]
        self.candles.set_data(ohlc)

        num = den = 0.0
        vwap = np.empty(n, dtype=float)
        for i, b in enumerate(bars):
            num += b["vwap_num"]
            den += b["vwap_den"]
            vwap[i] = num / den if den > 0 else b["c"]
        self.vwap_curve.setData(np.arange(n), vwap)

        totals = {}
        for b in bars:
            for p, (bv, sv) in b["levels"].items():
                totals[p] = totals.get(p, 0.0) + bv + sv
        if totals:
            poc = max(totals.items(), key=lambda kv: kv[1])[0]
            self.poc_line.setValue(poc)

        deltas = np.array([b["buy"] - b["sell"] for b in bars], dtype=float)
        brushes_d = [GREEN if d >= 0 else RED for d in deltas]
        self.delta_bars.setOpts(
            x=np.arange(n), height=deltas, width=0.7, brushes=brushes_d
        )

        cvd_per_bar = np.cumsum(deltas)
        self.cvd_curve.setData(np.arange(n), cvd_per_bar)

        if self.show_bubbles and trades:
            bar_ts = np.array([b["ts"] for b in bars])
            interval_ms = self.interval_sec * 1000
            xs, ys, sizes, brushes_b = [], [], [], []
            # 仅取最近 500 笔以控制点数
            for t in list(trades)[-500:]:
                ts_ms = t["ts"]
                ts_s = ts_ms // 1000
                idx = int(np.searchsorted(bar_ts, ts_s, side="right") - 1)
                if idx < 0 or idx >= n:
                    continue
                frac = (ts_ms - bar_ts[idx] * 1000) / interval_ms
                frac = min(max(frac, 0.0), 0.95)
                xs.append(idx - 0.35 + frac * 0.7)
                ys.append(t["p"])
                sizes.append(float(min(5.0 + np.sqrt(max(t["v"], 0.0)) * 2.2, 42.0)))
                col = QColor(GREEN if t["side"] == 1 else RED)
                col.setAlpha(170)
                brushes_b.append(QBrush(col))
            self.bubbles.setData(x=xs, y=ys, size=sizes, brush=brushes_b, pen=None)
        elif not self.show_bubbles:
            self.bubbles.setData(x=[], y=[])


# ---------------------------------------------------------------------------
class DistributionPanel(QWidget):
    """横向量价分布 + 均值 / ±1σ / ±2σ + 正态拟合曲线."""

    def __init__(self):
        super().__init__()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        self.plot = pg.PlotWidget(
            title="Volume × Price Distribution  (量价分布 + 正态拟合)"
        )
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.setLabel("left", "Price")
        self.plot.setLabel("bottom", "Volume")

        self.profile_curve = self.plot.plot(pen=pg.mkPen(BLUE, width=2))
        self.gauss_curve = self.plot.plot(
            pen=pg.mkPen(ORANGE, width=1, style=Qt.PenStyle.DashLine)
        )

        self.mean_line = pg.InfiniteLine(
            angle=0, pen=pg.mkPen(YELLOW, width=1)
        )
        self.plot.addItem(self.mean_line)
        self.sigma_lines = []
        for _ in range(4):
            line = pg.InfiniteLine(
                angle=0, pen=pg.mkPen(GREY, width=1, style=Qt.PenStyle.DashLine)
            )
            self.sigma_lines.append(line)
            self.plot.addItem(line)

        root.addWidget(self.plot)

    def update_panel(self, bars):
        if not bars:
            return
        totals = defaultdict(float)
        for b in bars:
            for p, (bv, sv) in b["levels"].items():
                totals[p] += bv + sv
        if not totals:
            return
        prices = np.array(sorted(totals.keys()), dtype=float)
        vols = np.array([totals[p] for p in prices], dtype=float)
        if prices.size < 2 or vols.sum() <= 0:
            return

        self.profile_curve.setData(vols, prices)

        total_v = vols.sum()
        mean = float((prices * vols).sum() / total_v)
        var = float(((prices - mean) ** 2 * vols).sum() / total_v)
        sigma = float(np.sqrt(var))

        self.mean_line.setValue(mean)
        for line, k in zip(self.sigma_lines, (-2, -1, 1, 2)):
            line.setValue(mean + k * sigma)

        if sigma > 0:
            gy = np.linspace(prices.min(), prices.max(), 240)
            peak = vols.max()
            gx = peak * np.exp(-0.5 * ((gy - mean) / sigma) ** 2)
            self.gauss_curve.setData(gx, gy)
        else:
            self.gauss_curve.setData([], [])


# ---------------------------------------------------------------------------
class SignalPanel(QWidget):
    """模式下拉 + 自动下单开关 + 手动下单按钮 + Bridge 状态 + PnL 汇总.

    place_clicked 回传 (signal, mode) — mode = 'paper' / 'live'.
    """

    place_clicked = pyqtSignal(object, str)

    def __init__(self, config):
        super().__init__()
        self.cfg = config
        self.current_signal = None

        self.setStyleSheet("QWidget{background:#12121a;}")
        frame = QFrame(self)
        frame.setFrameShape(QFrame.Shape.Box)
        frame.setStyleSheet("QFrame{border:1px solid #333;border-radius:4px;}")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)

        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        header = QLabel("🎯  SIGNAL")
        header.setStyleSheet("color:#ffcc33;font-weight:bold;font-size:13px;border:none;")
        layout.addWidget(header)

        self.kind_lbl = QLabel("等待信号…")
        self.kind_lbl.setStyleSheet("color:#888;font-size:12px;border:none;")
        layout.addWidget(self.kind_lbl)

        self.stats_lbl = QLabel("")
        self.stats_lbl.setFont(QFont("Consolas", 9))
        self.stats_lbl.setStyleSheet("color:#d0d0d0;border:none;")
        layout.addWidget(self.stats_lbl)

        self.reasons_lbl = QLabel("")
        self.reasons_lbl.setStyleSheet("color:#42a5f5;font-size:10px;border:none;")
        self.reasons_lbl.setWordWrap(True)
        layout.addWidget(self.reasons_lbl)

        # 模式 + 自动下单
        mode_row = QHBoxLayout()
        mode_row.setContentsMargins(0, 4, 0, 0)
        mode_row.setSpacing(6)

        mode_lbl = QLabel("模式:")
        mode_lbl.setStyleSheet("color:#888;border:none;")
        mode_row.addWidget(mode_lbl)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("📝 纸面", "paper")
        self.mode_combo.addItem("🔴 DOM 实盘", "live")
        if config.trading.default_live:
            self.mode_combo.setCurrentIndex(1)
        self.mode_combo.setStyleSheet(
            "QComboBox{background:#1a1a22;color:#d0d0d0;padding:3px;border:1px solid #333;}"
        )
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self.mode_combo, 1)

        self.auto_chk = QCheckBox("自动下单")
        self.auto_chk.setStyleSheet("color:#ffcc33;border:none;font-weight:bold;")
        mode_row.addWidget(self.auto_chk)
        layout.addLayout(mode_row)

        # 手动下单按钮 (按选中模式)
        self.place_btn = QPushButton("手动下单")
        self.place_btn.setEnabled(False)
        self.place_btn.clicked.connect(self._on_manual)
        layout.addWidget(self.place_btn)
        self._apply_btn_style()

        self.bridge_lbl = QLabel("● Bridge: 未连接")
        self.bridge_lbl.setStyleSheet("color:#ef5350;font-size:10px;border:none;")
        layout.addWidget(self.bridge_lbl)

        self.ccxt_lbl = QLabel("● ccxt: 未就绪")
        self.ccxt_lbl.setStyleSheet("color:#ef5350;font-size:10px;border:none;")
        layout.addWidget(self.ccxt_lbl)

        self.paper_pnl_lbl = QLabel("Paper: +0.00   W/L 0/0")
        self.paper_pnl_lbl.setStyleSheet("color:#d0d0d0;font-size:10px;border:none;font-family:Consolas;")
        layout.addWidget(self.paper_pnl_lbl)

        self.live_pnl_lbl = QLabel("Live : +0.00   W/L 0/0")
        self.live_pnl_lbl.setStyleSheet("color:#d0d0d0;font-size:10px;border:none;font-family:Consolas;")
        layout.addWidget(self.live_pnl_lbl)

        layout.addStretch(1)

    def update_signal(self, signal):
        self.current_signal = signal
        if signal is None:
            self.kind_lbl.setText("等待信号…")
            self.kind_lbl.setStyleSheet("color:#888;font-size:12px;border:none;")
            self.stats_lbl.setText("")
            self.reasons_lbl.setText("")
            self.place_btn.setEnabled(False)
            return
        color = GREEN if signal.side == "buy" else RED
        self.kind_lbl.setText(f"{signal.kind.upper()}   [score {signal.score}/4]")
        self.kind_lbl.setStyleSheet(
            f"color:{color};font-weight:bold;font-size:12px;border:none;"
        )
        self.stats_lbl.setText(
            f"Side   : {signal.side.upper()}\n"
            f"Entry  : {signal.entry:.4f}\n"
            f"Stop   : {signal.stop:.4f}   (-{abs(signal.entry-signal.stop):.4f})\n"
            f"Target : {signal.target:.4f}   (+{abs(signal.target-signal.entry):.4f})\n"
            f"RR     : {signal.rr:.2f}\n"
            f"Qty    : {signal.qty:.0f} 张"
        )
        self.reasons_lbl.setText("  •  ".join(signal.reasons))
        self.place_btn.setEnabled(True)

    def selected_mode(self) -> str:
        return self.mode_combo.currentData() or "paper"

    def auto_enabled(self) -> bool:
        return self.auto_chk.isChecked()

    def _on_mode_changed(self):
        self._apply_btn_style()

    def _apply_btn_style(self):
        if self.selected_mode() == "live":
            self.place_btn.setText("🔴 手动 DOM 下单")
            self.place_btn.setStyleSheet(
                "QPushButton{background:#222;color:#666;padding:6px;border:1px solid #333;}"
                "QPushButton:enabled{background:#4a1a1a;color:#fff;border:1px solid #ef5350;font-weight:bold;}"
                "QPushButton:hover:enabled{background:#7a2a2a;}"
            )
        else:
            self.place_btn.setText("📝 手动纸面下单")
            self.place_btn.setStyleSheet(
                "QPushButton{background:#222;color:#666;padding:6px;border:1px solid #333;}"
                "QPushButton:enabled{background:#1f3a2a;color:#fff;border:1px solid #26a69a;}"
                "QPushButton:hover:enabled{background:#2a5a3a;}"
            )

    def _on_manual(self):
        if self.current_signal is not None:
            self.place_clicked.emit(self.current_signal, self.selected_mode())

    def set_bridge_connected(self, connected: bool):
        if connected:
            self.bridge_lbl.setText("● Bridge: 已连接")
            self.bridge_lbl.setStyleSheet("color:#26a69a;font-size:10px;border:none;")
        else:
            self.bridge_lbl.setText("● Bridge: 未连接")
            self.bridge_lbl.setStyleSheet("color:#ef5350;font-size:10px;border:none;")

    def set_ccxt_ready(self, ready: bool):
        if ready:
            self.ccxt_lbl.setText("● ccxt: 就绪 (Live 反手可用)")
            self.ccxt_lbl.setStyleSheet("color:#26a69a;font-size:10px;border:none;")
        else:
            self.ccxt_lbl.setText("● ccxt: 未就绪 (Live 锁仓)")
            self.ccxt_lbl.setStyleSheet("color:#ef5350;font-size:10px;border:none;")

    def set_pnl(self, paper_pnl, paper_w, paper_l, live_pnl, live_w, live_l):
        def _line(label, pnl, w, l):
            color = GREEN if pnl >= 0 else RED
            total = w + l
            wr = (100.0 * w / total) if total > 0 else 0.0
            return (
                f'{label}: <span style="color:{color}">{pnl:+.2f}</span>   '
                f'W/L {w}/{l}   WR {wr:.0f}%'
            )
        self.paper_pnl_lbl.setText(_line("Paper", paper_pnl, paper_w, paper_l))
        self.live_pnl_lbl.setText(_line("Live ", live_pnl, live_w, live_l))



# ---------------------------------------------------------------------------
class OrderLogPanel(QWidget):
    """下单日志 + 收益记录. 显示最近 N 条订单, 平仓后就地更新.

    Orders 以 deque 存 dict, 格式:
        {ts, mode, side, kind, qty, entry, stop, target, status,
         exit_price, pnl, closed_ts}
    status: OPEN / WIN / LOSS / FAIL
    """

    def __init__(self, maxlen: int = 200):
        super().__init__()
        self.orders = deque(maxlen=maxlen)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        frame = QFrame(self)
        frame.setFrameShape(QFrame.Shape.Box)
        frame.setStyleSheet("QFrame{border:1px solid #333;border-radius:4px;}")
        outer.addWidget(frame)

        layout = QVBoxLayout(frame)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        header = QLabel("📒  ORDER LOG")
        header.setStyleSheet("color:#ffcc33;font-weight:bold;font-size:13px;border:none;")
        layout.addWidget(header)

        self.view = QTextEdit()
        self.view.setReadOnly(True)
        self.view.setFont(QFont("Consolas", 9))
        self.view.setStyleSheet(
            "background:#0e0e12;color:#d0d0d0;border:1px solid #222;"
        )
        layout.addWidget(self.view)

    def add(self, order: dict):
        self.orders.appendleft(order)
        self.rebuild()

    def update(self):
        self.rebuild()

    def rebuild(self):
        lines = []
        for o in self.orders:
            ts = time.strftime("%H:%M:%S", time.localtime(o["ts"] / 1000))
            side_str = "BUY " if o["side"] == "buy" else "SELL"
            side_color = GREEN if o["side"] == "buy" else RED
            mode_tag = "LIVE" if o["mode"] == "live" else "PAPR"
            mode_color = RED if o["mode"] == "live" else BLUE
            status = o["status"]
            if status == "OPEN":
                status_html = (
                    f'<span style="color:#ffcc33">[OPEN]</span>'
                )
                pnl_html = ""
            elif status == "WIN":
                status_html = f'<span style="color:{GREEN}">[TP]</span>'
                pnl_html = f' <span style="color:{GREEN}">{o["pnl"]:+.2f}</span>'
            elif status == "LOSS":
                status_html = f'<span style="color:{RED}">[SL]</span>'
                pnl_html = f' <span style="color:{RED}">{o["pnl"]:+.2f}</span>'
            else:  # FAIL
                status_html = f'<span style="color:#888">[FAIL]</span>'
                pnl_html = ""
            exit_txt = f' → {o["exit_price"]:.4f}' if o.get("exit_price") else ""
            lines.append(
                f'<span style="color:#888">{ts}</span> '
                f'<span style="color:{mode_color};font-weight:bold">{mode_tag}</span> '
                f'<span style="color:{side_color}">{side_str}</span> '
                f'{o["qty"]:.0f}@{o["entry"]:.4f}'
                f'{exit_txt}  {status_html}{pnl_html}'
            )
        self.view.setHtml("<br>".join(lines))


# ---------------------------------------------------------------------------
class Dashboard(QMainWindow):
    def __init__(self, store, config=None, strategy=None, bridge=None, exchange=None):
        super().__init__()
        self.store = store
        self.config = config
        self.strategy = strategy
        self.bridge = bridge
        self.exchange = exchange
        self.setWindowTitle(f"MEXC Scalper — {store.symbol}")
        self.resize(1720, 960)

        self._big_trades = deque(maxlen=120)
        self._last_slow_refresh = 0.0
        self._last_dist_refresh = 0.0
        self._last_bar_ts = {"1s": 0, "1m": 0, "5m": 0}
        self._tape_dirty = False

        # 仓位 / 结果统计 (paper + live 分开计)
        self._open_pos = None     # dict: side, entry, stop, target, qty, mode, order, ...
        self._paper_pnl = 0.0
        self._live_pnl = 0.0
        self._paper_w = 0
        self._paper_l = 0
        self._live_w = 0
        self._live_l = 0
        self._last_signal_kind = None
        self._log_file = Path(__file__).parent / "trade_log.jsonl"

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(4, 4, 4, 4)

        # 左: 三个时间周期 Tab
        self.tabs = QTabWidget()
        self.panel_1s = TimeframePanel("1s", 1, show_bubbles=True)
        self.panel_1m = TimeframePanel("1m", 60)
        self.panel_5m = TimeframePanel("5m", 300)
        self.tabs.addTab(self.panel_1s, "Scalp 1s")
        self.tabs.addTab(self.panel_1m, "1m")
        self.tabs.addTab(self.panel_5m, "5m")

        # 右列: Signal / 分布 / DOM / Tape
        right = QSplitter(Qt.Orientation.Vertical)

        self.signal_panel = SignalPanel(config) if config is not None else None
        if self.signal_panel is not None:
            self.signal_panel.place_clicked.connect(self._on_place)
            right.addWidget(self.signal_panel)

        self.order_log = OrderLogPanel()
        right.addWidget(self.order_log)

        self.dist_panel = DistributionPanel()
        right.addWidget(self.dist_panel)

        self.dom_plot = pg.PlotWidget(title="Order Book  20 档 (绿买 / 红卖)")
        self.dom_plot.showGrid(x=True, y=True, alpha=0.15)
        self.dom_plot.setLabel("bottom", "Price")
        self.dom_plot.setLabel("left", "Size")
        self.bid_bars = pg.BarGraphItem(x=[0], height=[0], width=0.1, brush=GREEN)
        self.ask_bars = pg.BarGraphItem(x=[0], height=[0], width=0.1, brush=RED)
        self.dom_plot.addItem(self.bid_bars)
        self.dom_plot.addItem(self.ask_bars)
        self.last_line = pg.InfiniteLine(
            angle=90, pen=pg.mkPen(YELLOW, width=1, style=Qt.PenStyle.DashLine)
        )
        self.dom_plot.addItem(self.last_line)
        right.addWidget(self.dom_plot)

        self.tape = QTextEdit()
        self.tape.setReadOnly(True)
        self.tape.setFont(QFont("Consolas", 10))
        self.tape.setStyleSheet(
            "background:#0e0e12;color:#d0d0d0;border:1px solid #222;"
        )
        right.addWidget(self.tape)

        if self.signal_panel is not None:
            right.setSizes([240, 240, 220, 220, 180])
        else:
            right.setSizes([340, 340, 260])

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.tabs)
        splitter.addWidget(right)
        splitter.setSizes([1170, 550])
        root.addWidget(splitter)

        self.status = self.statusBar()
        self.status.showMessage("connecting MEXC…")

        self.store.big_trade.connect(self._on_big_trade)

        # 快定时器: 轻量更新 (DOM / 最新价 / 状态栏)
        self._fast_timer = QTimer()
        self._fast_timer.timeout.connect(self._refresh_fast)
        self._fast_timer.start(120)
        # 慢定时器: 蜡烛 / Delta / CVD / 泡泡 / 分布
        self._slow_timer = QTimer()
        self._slow_timer.timeout.connect(self._refresh_slow)
        self._slow_timer.start(400)

    # ------------------------------------------------------------------
    def _refresh_slow(self):
        """蜡烛 / Delta / CVD / 泡泡 / 分布. ~2.5 Hz."""
        s = self.store
        idx = self.tabs.currentIndex()
        panel_map = [
            (self.panel_1s, "1s", s.trades),
            (self.panel_1m, "1m", None),
            (self.panel_5m, "5m", None),
        ]
        panel, key, trades = panel_map[idx]
        bars_list = list(s.bars[key])
        # 只在 bar 关闭或新 bar 产生时重画 (当前 bar 仍高频变动, 由收敛到下秒处理)
        if bars_list:
            last_ts = bars_list[-1]["ts"]
            if last_ts != self._last_bar_ts[key] or True:
                # 保留 or True: 当前 bar 内部也更新, 但 400ms 节奏够平滑
                panel.update_panel(bars_list, trades=trades)
                self._last_bar_ts[key] = last_ts

        # Distribution 再降一档到 ~1 Hz, 聚合最贵
        now = time.monotonic()
        if now - self._last_dist_refresh > 1.0:
            self.dist_panel.update_panel(list(s.bars["1s"]))
            self._last_dist_refresh = now

        # 策略评估 (~2.5 Hz)
        if self.strategy is not None and self.signal_panel is not None:
            sig = self.strategy.evaluate()
            self.signal_panel.update_signal(sig)
            if sig is not None and sig.kind != self._last_signal_kind:
                self._last_signal_kind = sig.kind
                _async_beep(1 if sig.side == "buy" else 2)
                # 自动下单: 交给 _on_place 内部的反手/锁仓逻辑裁决
                if self.signal_panel.auto_enabled():
                    self._on_place(sig, self.signal_panel.selected_mode())

    def _refresh_fast(self):
        """DOM / 最新价 / 状态栏. ~8 Hz, 不碰蜡烛."""
        s = self.store
        if s.bids and s.asks:
            bid_px = np.array([p for p, _ in s.bids])
            bid_vol = np.array([v for _, v in s.bids])
            ask_px = np.array([p for p, _ in s.asks])
            ask_vol = np.array([v for _, v in s.asks])
            all_px = np.sort(np.concatenate([bid_px, ask_px]))
            if len(all_px) > 1:
                diffs = np.diff(all_px)
                diffs = diffs[diffs > 0]
                tick = diffs.min() if len(diffs) else 0.1
            else:
                tick = 0.1
            w = tick * 0.9
            self.bid_bars.setOpts(x=bid_px, height=bid_vol, width=w, brush=GREEN)
            self.ask_bars.setOpts(x=ask_px, height=ask_vol, width=w, brush=RED)
            if s.last_price is not None:
                self.last_line.setValue(s.last_price)

        last = s.last_price or 0.0
        cvd_v = s.cvd[-1][1] if s.cvd else 0.0
        bars_1s = s.bars["1s"]
        cur_delta = (bars_1s[-1]["buy"] - bars_1s[-1]["sell"]) if bars_1s else 0.0
        self.status.showMessage(
            f"{s.symbol}  last={last:.4f}  CVD={cvd_v:+.1f}  "
            f"curΔ={cur_delta:+.1f}  "
            f"bars(1s/1m/5m)={len(s.bars['1s'])}/{len(s.bars['1m'])}/{len(s.bars['5m'])}  "
            f"trades={len(s.trades)}"
        )

        if self._tape_dirty:
            self._rebuild_tape()
            self._tape_dirty = False

        # 持仓盯盘: 到达止损/目标就结算
        self._check_position(s.last_price)

        # Bridge / ccxt 状态指示
        if self.signal_panel is not None:
            if self.bridge is not None:
                self.signal_panel.set_bridge_connected(self.bridge.connected)
            if self.exchange is not None:
                self.signal_panel.set_ccxt_ready(self.exchange.ready)

    # ------------------------------------------------------------------
    def _on_big_trade(self, t: dict):
        # 不立刻重绘 Tape, 仅标记脏; fast timer 合批
        self._big_trades.append(t)
        self._tape_dirty = True
        _async_beep(t["side"])

    # ------------------------------------------------------------------
    def _on_place(self, signal, mode: str):
        """mode: 'paper' 或 'live'.

        反手规则:
          - 无持仓 -> 直接开
          - 同向   -> 静默忽略 (已经在场内)
          - 反向   -> 只有 paper↔paper 允许平旧开新 (用 last_price 结算);
                      其它组合拒绝 (live 有孤儿 trigger 风险)
        """
        if self._open_pos is not None:
            cur = self._open_pos
            if cur["side"] == signal.side:
                return
            if cur["mode"] == "paper" and mode == "paper":
                last = self.store.last_price
                if last is None:
                    self._toast("反手失败: 无最新价")
                    return
                self._settle_position(last, "REVERSE")
                # 落到下面正常开仓路径
            elif cur["mode"] == "live" and mode == "live":
                # Live 反手: 需要 ccxt 撤掉孤儿 SL/TP 才安全
                if self.exchange is None or not self.exchange.ready:
                    self._toast("ccxt 未就绪, Live 反手被拒")
                    return
                asyncio.ensure_future(self._live_reverse(signal))
                return
            else:
                self._toast("跨模式反手不支持")
                return

        if mode == "live":
            if self.bridge is None or not self.bridge.connected:
                self._toast("扩展未连接, 拒绝 Live 下单")
                return

        order = {
            "ts": int(time.time() * 1000),
            "mode": mode,
            "side": signal.side,
            "kind": signal.kind,
            "qty": float(signal.qty),
            "entry": float(signal.entry),
            "stop": float(signal.stop),
            "target": float(signal.target),
            "rr": float(signal.rr),
            "status": "OPEN",
            "exit_price": None,
            "pnl": 0.0,
            "closed_ts": None,
        }
        self.order_log.add(order)
        self._write_log(order)

        self._open_pos = {
            "side": signal.side, "kind": signal.kind,
            "entry": signal.entry, "stop": signal.stop, "target": signal.target,
            "qty": signal.qty, "mode": mode,
            "opened": time.time(),
            "order": order,
        }

        if mode == "live":
            asyncio.ensure_future(self._live_place(signal, order))
        else:
            self._toast(f"[PAPER] {signal.side.upper()} {signal.qty:.0f}@{signal.entry:.4f}")

    async def _live_reverse(self, new_signal):
        """Live 反手: ccxt 撤孤儿单 -> bridge 平仓 -> 本地结算 -> 开新仓."""
        cur = self._open_pos
        if cur is None or cur["mode"] != "live":
            return

        # 1) ccxt 撤所有挂单 + trigger
        n = await self.exchange.cancel_all_orders()
        self._toast(f"[REVERSE] ccxt 撤销 {n} 单")

        # 2) bridge 平仓
        close_side = "closeLong" if cur["side"] == "buy" else "closeShort"
        res = await self.bridge.close_position(close_side, float(cur["qty"]))
        if not res.get("success"):
            self._toast(f"[REVERSE FAIL] close: {res.get('error', '?')}")
            return

        # 3) 本地结算旧仓 (按最新价)
        last = self.store.last_price or cur["entry"]
        self._settle_position(last, "REVERSE")

        # 4) 开新仓: 复用 _on_place 正常路径
        self._on_place(new_signal, "live")

    async def _live_place(self, signal, order: dict):
        # 1) 入场限价
        res = await self.bridge.place_limit_order(
            side=signal.side,
            amount=float(signal.qty),
            price=float(signal.entry),
        )
        if not res.get("success"):
            err = res.get("error", "?")
            self._toast(f"[LIVE FAIL 入场] {err}")
            order["status"] = "FAIL"
            self.order_log.update()
            self._write_log(order)
            self._open_pos = None
            return

        self._toast(
            f"[LIVE OK] 入场 {signal.side.upper()} {signal.qty:.0f}@{signal.entry:.4f}"
        )

        # 2) 立刻挂反向 SL + TP 条件单 (要求账户为单向持仓)
        opp = "sell" if signal.side == "buy" else "buy"
        qty = float(signal.qty)

        sl_res = await self.bridge.place_trigger_order(
            side=opp, amount=qty,
            trigger_price=float(signal.stop),
            price=float(signal.stop),
        )
        tp_res = await self.bridge.place_trigger_order(
            side=opp, amount=qty,
            trigger_price=float(signal.target),
            price=float(signal.target),
        )

        sl_ok = sl_res.get("success", False)
        tp_ok = tp_res.get("success", False)
        order["sl_placed"] = sl_ok
        order["tp_placed"] = tp_ok

        if sl_ok and tp_ok:
            self._toast(
                f"[LIVE OK] SL@{signal.stop:.4f}  TP@{signal.target:.4f}  已挂单"
            )
        else:
            msg_sl = "SL✓" if sl_ok else f"SL✗({sl_res.get('error','?')})"
            msg_tp = "TP✓" if tp_ok else f"TP✗({tp_res.get('error','?')})"
            self._toast(f"[LIVE WARN] {msg_sl}  {msg_tp}")

        self.order_log.update()
        self._write_log(order)

    def _check_position(self, last_price):
        pos = self._open_pos
        if pos is None or last_price is None:
            return
        side = pos["side"]
        hit_stop = (side == "buy" and last_price <= pos["stop"]) or \
                   (side == "sell" and last_price >= pos["stop"])
        hit_target = (side == "buy" and last_price >= pos["target"]) or \
                     (side == "sell" and last_price <= pos["target"])
        if not (hit_stop or hit_target):
            return
        exit_price = pos["stop"] if hit_stop else pos["target"]
        reason = "TP" if hit_target else "SL"
        self._settle_position(exit_price, reason)

    def _settle_position(self, exit_price: float, reason: str):
        """结算当前持仓. reason: TP / SL / REVERSE / MANUAL."""
        pos = self._open_pos
        if pos is None:
            return
        side = pos["side"]
        mult = self.config.trading.contract_multiplier if self.config else 0.0001
        if side == "buy":
            pnl = (exit_price - pos["entry"]) * pos["qty"] * mult
        else:
            pnl = (pos["entry"] - exit_price) * pos["qty"] * mult

        mode = pos["mode"]
        is_win = pnl >= 0
        if mode == "live":
            self._live_pnl += pnl
            if is_win: self._live_w += 1
            else: self._live_l += 1
        else:
            self._paper_pnl += pnl
            if is_win: self._paper_w += 1
            else: self._paper_l += 1

        order = pos.get("order")
        if order is not None:
            order["status"] = "WIN" if is_win else "LOSS"
            order["exit_price"] = exit_price
            order["pnl"] = pnl
            order["closed_ts"] = int(time.time() * 1000)
            order["close_reason"] = reason
            self.order_log.update()
            self._write_log(order)

        self._toast(
            f"[{mode.upper()} {reason}] {side.upper()} -> {exit_price:.4f}  PnL {pnl:+.2f}"
        )

        if mode == "live" and self.bridge is not None and self.bridge.connected:
            close_side = "closeLong" if side == "buy" else "closeShort"
            asyncio.ensure_future(
                self.bridge.close_position(close_side, float(pos["qty"]))
            )

        if self.strategy is not None:
            self.strategy.register_result(pnl)
        if self.signal_panel is not None:
            self.signal_panel.set_pnl(
                self._paper_pnl, self._paper_w, self._paper_l,
                self._live_pnl, self._live_w, self._live_l,
            )
        self._open_pos = None

    def _write_log(self, order: dict):
        """追加一条订单事件到 trade_log.jsonl (开仓/平仓/失败都写)."""
        try:
            with open(self._log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(order, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[log] write failed: {e}")

    def _toast(self, text: str):
        self.status.showMessage(text, 4000)
        print(text)

    def _rebuild_tape(self):
        lines = []
        for bt in reversed(self._big_trades):
            side_txt = "BUY " if bt["side"] == 1 else "SELL"
            color = GREEN if bt["side"] == 1 else RED
            ts = time.strftime("%H:%M:%S", time.localtime(bt["ts"] / 1000))
            lines.append(
                f'<span style="color:{color};font-weight:bold">'
                f'[{ts}] {side_txt} {bt["v"]:>8.1f} @ {bt["p"]:.4f}'
                f"</span>"
            )
        self.tape.setHtml("<br>".join(lines))


def _async_beep(side: int):
    """非阻塞蜂鸣: MessageBeep 即时返回, 不会卡主线程."""
    try:
        import winsound
        # MessageBeep 是异步播放系统声音, 不阻塞
        flag = winsound.MB_ICONASTERISK if side == 1 else winsound.MB_ICONHAND
        winsound.MessageBeep(flag)
    except Exception:
        pass
