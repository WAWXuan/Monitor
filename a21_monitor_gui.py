"""
A21 Particle Count Monitor — GUI 版本
实时监控 TSI APS .A21 文件，显示 +56 offset 计数曲线并检测稳定状态

依赖: pip install matplotlib watchdog numpy
运行: python a21_monitor_gui.py
"""

import sys
import time
import struct
import re
import threading
import queue
from pathlib import Path
from datetime import datetime

import numpy as np
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import matplotlib

matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except ImportError:
    print("请安装依赖: pip install matplotlib watchdog numpy")
    sys.exit(1)


# ── 稳定检测参数 ────────────────────────────────────────────
WINDOW = 10
CV_THRESH = 0.015
SLOPE_THRESH = 400
MIN_RECORDS = 25
CONSECUTIVE = 3
POLL_SEC = 2.0
# ───────────────────────────────────────────────────────────

PATTERN = b"Model 3321 Firmware"
OFFSET = 56


# ── 解析 ────────────────────────────────────────────────────


def parse_a21(path: Path):
    try:
        data = path.read_bytes()
    except Exception:
        return []
    positions = [m.start() for m in re.finditer(re.escape(PATTERN), data)]
    counts = []
    for p in positions:
        if p + OFFSET + 4 <= len(data):
            counts.append(struct.unpack_from("<I", data, p + OFFSET)[0])
    return counts


def compute_window_stats(counts, window=WINDOW):
    n = len(counts)
    if n < 2:
        return None, None
    w = counts[max(0, n - window) :]
    mean = np.mean(w)
    cv = np.std(w) / mean if mean > 0 else 999
    slope = np.polyfit(range(len(w)), w, 1)[0]
    return cv, slope


# ── 文件监视器 ───────────────────────────────────────────────


class A21Watcher:
    """监控单文件或目录（跟踪最新 A21），通过 queue 推送新数据"""

    def __init__(self, update_queue: queue.Queue):
        self.q = update_queue
        self.mode = None  # 'file' or 'folder'
        self.target_file = None  # 当前监控的文件
        self.watch_dir = None
        self._observer = None
        self._poll_thread = None
        self._stop_evt = threading.Event()
        self._last_count = 0
        self._last_mtime = 0

    def start_file(self, path: Path):
        self.mode = "file"
        self.target_file = path
        self._restart()

    def start_folder(self, folder: Path):
        self.mode = "folder"
        self.watch_dir = folder
        self.target_file = self._latest_a21(folder)
        self._restart()

    def _latest_a21(self, folder: Path):
        files = list(folder.glob("*.A21")) + list(folder.glob("*.a21"))
        if not files:
            return None
        return max(files, key=lambda p: p.stat().st_mtime)

    def stop(self):
        self._stop_evt.set()
        if self._observer:
            self._observer.stop()
            self._observer.join()

    def _restart(self):
        self.stop()
        self._stop_evt.clear()
        self._last_count = 0
        self._last_mtime = 0

        handler = _FSHandler(self)

        self._observer = Observer()
        watch_path = (
            self.watch_dir if self.mode == "folder" else self.target_file.parent
        )
        self._observer.schedule(handler, str(watch_path), recursive=False)
        self._observer.start()

        # 轮询线程（补充 watchdog 可能漏掉的事件）
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

        # 立即处理一次
        self._check()

    def _poll_loop(self):
        while not self._stop_evt.wait(POLL_SEC):
            self._check()

    def _check(self):
        # 文件夹模式：检查是否有更新的 A21
        if self.mode == "folder" and self.watch_dir:
            latest = self._latest_a21(self.watch_dir)
            if latest and latest != self.target_file:
                self.target_file = latest
                self._last_count = 0
                self._last_mtime = 0
                self.q.put(("new_file", str(latest)))

        if not self.target_file or not self.target_file.exists():
            return

        try:
            mtime = self.target_file.stat().st_mtime
        except Exception:
            return

        if mtime == self._last_mtime:
            return
        self._last_mtime = mtime

        counts = parse_a21(self.target_file)
        if len(counts) != self._last_count:
            self._last_count = len(counts)
            self.q.put(("data", counts, str(self.target_file)))

    def notify_fs(self):
        self._check()


class _FSHandler(FileSystemEventHandler):
    def __init__(self, watcher: A21Watcher):
        self.watcher = watcher

    def on_modified(self, event):
        if not event.is_directory:
            self.watcher.notify_fs()

    def on_created(self, event):
        if not event.is_directory:
            self.watcher.notify_fs()


# ── 主 GUI ──────────────────────────────────────────────────


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("APS A21 实时监控")
        self.geometry("1000x680")
        self.configure(bg="#1e1e2e")

        self.q = queue.Queue()
        self.watcher = A21Watcher(self.q)

        self.counts = []
        self.cv_series = []
        self.slope_series = []
        self.stable_flags = []
        self.stable_triggered = False
        self.stable_rec = None
        self.streak = 0
        self.median_val = None

        self._build_ui()
        self.after(200, self._poll_queue)

    # ── UI ──────────────────────────────────────────────────

    def _build_ui(self):
        BG = "#1e1e2e"
        PANEL = "#2a2a3e"
        ACC = "#7f77dd"
        FG = "#cdd6f4"
        FONT = ("Segoe UI", 10)

        # ── 顶部控制栏 ──
        top = tk.Frame(self, bg=PANEL, pady=6)
        top.pack(fill="x", side="top")

        tk.Button(
            top,
            text="选择文件",
            command=self._pick_file,
            bg=ACC,
            fg="white",
            relief="flat",
            padx=12,
            font=FONT,
        ).pack(side="left", padx=(12, 4))
        tk.Button(
            top,
            text="选择文件夹",
            command=self._pick_folder,
            bg="#534AB7",
            fg="white",
            relief="flat",
            padx=12,
            font=FONT,
        ).pack(side="left", padx=4)

        self.lbl_file = tk.Label(
            top, text="未选择文件", bg=PANEL, fg="#888", font=FONT, anchor="w"
        )
        self.lbl_file.pack(side="left", padx=12, fill="x", expand=True)

        # 参数输入
        tk.Label(top, text="CV<", bg=PANEL, fg=FG, font=FONT).pack(side="left")
        self.var_cv = tk.StringVar(value=str(CV_THRESH))
        tk.Entry(
            top,
            textvariable=self.var_cv,
            width=6,
            bg="#3a3a5e",
            fg=FG,
            insertbackground=FG,
            relief="flat",
            font=FONT,
        ).pack(side="left", padx=(0, 8))

        tk.Label(top, text="|slope|<", bg=PANEL, fg=FG, font=FONT).pack(side="left")
        self.var_slope = tk.StringVar(value=str(SLOPE_THRESH))
        tk.Entry(
            top,
            textvariable=self.var_slope,
            width=6,
            bg="#3a3a5e",
            fg=FG,
            insertbackground=FG,
            relief="flat",
            font=FONT,
        ).pack(side="left", padx=(0, 8))

        tk.Label(top, text="连续", bg=PANEL, fg=FG, font=FONT).pack(side="left")
        self.var_consec = tk.StringVar(value=str(CONSECUTIVE))
        tk.Entry(
            top,
            textvariable=self.var_consec,
            width=4,
            bg="#3a3a5e",
            fg=FG,
            insertbackground=FG,
            relief="flat",
            font=FONT,
        ).pack(side="left", padx=(0, 12))

        # ── 状态栏 ──
        stat_row = tk.Frame(self, bg=PANEL, pady=4)
        stat_row.pack(fill="x")

        def make_stat(parent, label):
            f = tk.Frame(parent, bg=PANEL)
            f.pack(side="left", padx=16)
            tk.Label(f, text=label, bg=PANEL, fg="#888", font=("Segoe UI", 9)).pack()
            lbl = tk.Label(f, text="—", bg=PANEL, fg=FG, font=("Segoe UI", 13, "bold"))
            lbl.pack()
            return lbl

        self.stat_rec = make_stat(stat_row, "轮次")
        self.stat_val = make_stat(stat_row, "当前值")
        self.stat_cv = make_stat(stat_row, "CV")
        self.stat_slope = make_stat(stat_row, "斜率")
        self.stat_streak = make_stat(stat_row, "连续满足")
        self.stat_status = make_stat(stat_row, "状态")

        # ── 图表区 ──
        fig_frame = tk.Frame(self, bg=BG)
        fig_frame.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        self.fig = Figure(figsize=(12, 6), facecolor=BG)
        self.fig.subplots_adjust(
            hspace=0.35, left=0.07, right=0.97, top=0.93, bottom=0.08
        )

        # 主曲线
        self.ax1 = self.fig.add_subplot(3, 1, (1, 2))
        self.ax1.set_facecolor("#11111e")
        self.ax1.tick_params(colors="#888", labelsize=9)
        for sp in self.ax1.spines.values():
            sp.set_color("#333")
        self.ax1.set_ylabel("count (+56)", color="#888", fontsize=9)
        self.ax1.set_title("粒子计数实时曲线", color="#cdd6f4", fontsize=11, pad=8)

        # CV + slope
        self.ax2 = self.fig.add_subplot(3, 1, 3)
        self.ax2.set_facecolor("#11111e")
        self.ax2.tick_params(colors="#888", labelsize=9)
        for sp in self.ax2.spines.values():
            sp.set_color("#333")
        self.ax2.set_ylabel("CV / 斜率(k)", color="#888", fontsize=9)
        self.ax2.set_xlabel("轮次", color="#888", fontsize=9)

        self.canvas = FigureCanvasTkAgg(self.fig, master=fig_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── 文件选择 ────────────────────────────────────────────

    def _pick_file(self):
        path = filedialog.askopenfilename(
            title="选择 A21 文件",
            filetypes=[("APS Data", "*.A21 *.a21"), ("All files", "*.*")],
        )
        if path:
            self._reset_state()
            p = Path(path)
            self.lbl_file.config(text=str(p), fg="#cdd6f4")
            self.watcher.start_file(p)

    def _pick_folder(self):
        folder = filedialog.askdirectory(title="选择监控文件夹")
        if folder:
            self._reset_state()
            p = Path(folder)
            self.lbl_file.config(text=f"[文件夹] {p}", fg="#9fe1cb")
            self.watcher.start_folder(p)

    def _reset_state(self):
        self.counts = []
        self.cv_series = []
        self.slope_series = []
        self.stable_flags = []
        self.stable_triggered = False
        self.stable_rec = None
        self.streak = 0
        self.median_val = None

    # ── 队列轮询 ────────────────────────────────────────────

    def _poll_queue(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "data":
                    _, counts, fpath = msg
                    self._on_new_data(counts, fpath)
                elif msg[0] == "new_file":
                    self.lbl_file.config(text=f"[文件夹→] {msg[1]}", fg="#9fe1cb")
                    self._reset_state()
        except queue.Empty:
            pass
        self.after(200, self._poll_queue)

    # ── 数据处理 ────────────────────────────────────────────

    def _on_new_data(self, counts, fpath):
        if not counts:
            return

        self.counts = counts
        n = len(counts)

        # 参数（从输入框读）
        try:
            cv_t = float(self.var_cv.get())
            sl_t = float(self.var_slope.get())
            cons = int(self.var_consec.get())
        except ValueError:
            cv_t, sl_t, cons = CV_THRESH, SLOPE_THRESH, CONSECUTIVE

        # 计算每轮 cv / slope
        cvs, slopes, stables = [], [], []
        for i in range(n):
            if i < MIN_RECORDS - 1:
                cvs.append(None)
                slopes.append(None)
                stables.append(False)
                continue
            w = counts[max(0, i - WINDOW + 1) : i + 1]
            mean = np.mean(w)
            cv = np.std(w) / mean if mean > 0 else 999
            slp = np.polyfit(range(len(w)), w, 1)[0]
            if self.median_val is None:
                self.median_val = np.median(counts)
            above = counts[i] > self.median_val
            ok = cv < cv_t and abs(slp) < sl_t and above
            cvs.append(cv)
            slopes.append(slp)
            stables.append(ok)

        self.cv_series = cvs
        self.slope_series = slopes
        self.stable_flags = stables

        # 连续检测
        if not self.stable_triggered:
            streak = 0
            for i, ok in enumerate(stables):
                if ok:
                    streak += 1
                    if streak >= cons:
                        self.stable_triggered = True
                        self.stable_rec = i + 1
                        self.streak = streak
                        self._fire_notification(i + 1, counts[i], cvs[i])
                        break
                else:
                    streak = 0
            self.streak = streak

        # 更新状态栏
        latest_cv = next((c for c in reversed(cvs) if c is not None), None)
        latest_sl = next((s for s in reversed(slopes) if s is not None), None)
        cur_streak = 0
        for ok in reversed(stables):
            if ok:
                cur_streak += 1
            else:
                break

        self.stat_rec.config(text=str(n))
        self.stat_val.config(text=f"{counts[-1]:,}")
        self.stat_cv.config(
            text=f"{latest_cv:.4f}" if latest_cv else "—",
            fg="#ef9f27"
            if (latest_cv and latest_cv < cv_t)
            else "#ef4444"
            if latest_cv
            else "#888",
        )
        self.stat_slope.config(
            text=f"{latest_sl:+.0f}" if latest_sl else "—",
            fg="#ef9f27"
            if (latest_sl and abs(latest_sl) < sl_t)
            else "#ef4444"
            if latest_sl
            else "#888",
        )
        self.stat_streak.config(text=str(cur_streak))

        if self.stable_triggered:
            self.stat_status.config(
                text=f"✓ 稳定 (rec {self.stable_rec})", fg="#1d9e75"
            )
        elif n < MIN_RECORDS:
            self.stat_status.config(text="等待数据...", fg="#888")
        else:
            self.stat_status.config(text="上升中", fg="#ef9f27")

        self._redraw()

    # ── 图表绘制 ────────────────────────────────────────────

    def _redraw(self):
        counts = self.counts
        n = len(counts)
        xs = list(range(1, n + 1))

        try:
            cv_t = float(self.var_cv.get())
            sl_t = float(self.var_slope.get())
        except ValueError:
            cv_t, sl_t = CV_THRESH, SLOPE_THRESH

        # ── ax1: 主曲线 ──
        self.ax1.cla()
        self.ax1.set_facecolor("#11111e")
        self.ax1.tick_params(colors="#888", labelsize=9)
        for sp in self.ax1.spines.values():
            sp.set_color("#333")
        self.ax1.set_ylabel("count (+56)", color="#888", fontsize=9)
        self.ax1.set_title("粒子计数实时曲线", color="#cdd6f4", fontsize=11, pad=8)

        # 按状态着色背景
        if self.stable_rec:
            self.ax1.axvspan(
                self.stable_rec, n + 0.5, alpha=0.10, color="#1d9e75", lw=0
            )
            self.ax1.axvline(
                self.stable_rec, color="#1d9e75", lw=1.2, ls="--", alpha=0.8
            )

        # median 线
        if self.median_val:
            self.ax1.axhline(
                self.median_val,
                color="#534AB7",
                lw=0.8,
                ls=":",
                alpha=0.7,
                label="中位数",
            )

        # 主线
        self.ax1.plot(xs, counts, color="#7f77dd", lw=1.5, zorder=3)

        # 标记稳定点
        stable_xs = [xs[i] for i, ok in enumerate(self.stable_flags) if ok]
        stable_ys = [counts[i] for i, ok in enumerate(self.stable_flags) if ok]
        if stable_xs:
            self.ax1.scatter(
                stable_xs,
                stable_ys,
                color="#1d9e75",
                s=18,
                zorder=4,
                label="满足稳定条件",
            )

        # 触发点大标记
        if self.stable_rec:
            self.ax1.scatter(
                [self.stable_rec],
                [counts[self.stable_rec - 1]],
                color="#ef9f27",
                s=80,
                zorder=5,
                marker="*",
                label=f"触发通知 (rec {self.stable_rec})",
            )
            self.ax1.annotate(
                f"稳定\nrec {self.stable_rec}",
                xy=(self.stable_rec, counts[self.stable_rec - 1]),
                xytext=(min(self.stable_rec + 3, n), counts[self.stable_rec - 1]),
                color="#ef9f27",
                fontsize=8,
                arrowprops=dict(arrowstyle="->", color="#ef9f27", lw=0.8),
            )

        self.ax1.set_xlim(0.5, max(n + 0.5, 10))
        self.ax1.legend(
            fontsize=8,
            facecolor="#1e1e2e",
            labelcolor="#cdd6f4",
            framealpha=0.7,
            loc="upper left",
        )

        # ── ax2: CV + slope ──
        self.ax2.cla()
        self.ax2.set_facecolor("#11111e")
        self.ax2.tick_params(colors="#888", labelsize=9)
        for sp in self.ax2.spines.values():
            sp.set_color("#333")
        self.ax2.set_xlabel("轮次", color="#888", fontsize=9)

        valid_xs = [xs[i] for i, c in enumerate(self.cv_series) if c is not None]
        valid_cv = [c for c in self.cv_series if c is not None]
        valid_sl = [s / 10000 for s in self.slope_series if s is not None]  # scale

        if valid_xs:
            self.ax2.plot(valid_xs, valid_cv, color="#85b7eb", lw=1.2, label="CV")
            self.ax2.axhline(
                cv_t,
                color="#85b7eb",
                lw=0.7,
                ls="--",
                alpha=0.6,
                label=f"CV 阈值 {cv_t}",
            )

            ax2b = self.ax2.twinx()
            ax2b.set_facecolor("#11111e")
            ax2b.tick_params(colors="#888", labelsize=9)
            ax2b.plot(
                valid_xs,
                valid_sl,
                color="#ef9f27",
                lw=1.0,
                alpha=0.7,
                label="|slope| ×10⁻⁴",
            )
            sl_thresh_scaled = sl_t / 10000
            ax2b.axhline(sl_thresh_scaled, color="#ef9f27", lw=0.7, ls="--", alpha=0.5)
            ax2b.axhline(-sl_thresh_scaled, color="#ef9f27", lw=0.7, ls="--", alpha=0.5)
            ax2b.set_ylabel("slope ×10⁻⁴", color="#ef9f27", fontsize=8)
            ax2b.tick_params(colors="#888")
            for sp in ax2b.spines.values():
                sp.set_color("#333")

        if self.stable_rec:
            self.ax2.axvline(
                self.stable_rec, color="#1d9e75", lw=1.2, ls="--", alpha=0.8
            )

        self.ax2.set_xlim(0.5, max(n + 0.5, 10))
        self.ax2.legend(
            fontsize=8,
            facecolor="#1e1e2e",
            labelcolor="#cdd6f4",
            framealpha=0.7,
            loc="upper left",
        )
        self.ax2.set_ylabel("CV", color="#85b7eb", fontsize=9)

        self.canvas.draw_idle()

    # ── 通知 ────────────────────────────────────────────────

    def _fire_notification(self, rec: int, val: int, cv: float):
        def _do():
            msg = (
                f"粒子计数已进入稳定状态\n\n"
                f"轮次:   第 {rec} 轮\n"
                f"当前值: {val:,}\n"
                f"CV:     {cv:.4f}"
            )
            try:
                import winsound, ctypes

                for _ in range(3):
                    winsound.Beep(1000, 300)
                    time.sleep(0.15)
                ctypes.windll.user32.MessageBoxW(
                    0, msg, "APS 实验稳定通知", 0x40 | 0x40000
                )
            except Exception:
                self.after(0, lambda: messagebox.showinfo("稳定通知", msg))

        threading.Thread(target=_do, daemon=True).start()

    def _on_close(self):
        self.watcher.stop()
        self.destroy()


# ── 入口 ────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    # 命令行直接传路径
    if len(sys.argv) > 1:
        p = Path(sys.argv[1])
        if p.is_dir():
            app.watcher.start_folder(p)
            app.lbl_file.config(text=f"[文件夹] {p}", fg="#9fe1cb")
        elif p.exists():
            app.watcher.start_file(p)
            app.lbl_file.config(text=str(p), fg="#cdd6f4")
    app.mainloop()
