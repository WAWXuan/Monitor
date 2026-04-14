"""
APS A21 Real-Time Monitor
========================
Monitors a TSI APS Model 3321 .A21 file (or folder) in real time,
plots the undersize-bin count (record offset +56) with stability detection,
and fires a repeating audio alert until the user acknowledges it.

Requirements:
    pip install matplotlib watchdog numpy

Usage:
    python a21_monitor_gui.py                   # GUI file/folder picker
    python a21_monitor_gui.py path/to/file.A21  # direct file
    python a21_monitor_gui.py path/to/folder    # watch folder for newest A21
"""

import sys, time, struct, re, threading, queue, math
from pathlib import Path
from datetime import datetime, timedelta

import numpy as np
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import matplotlib

matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except ImportError:
    print("Missing dependency: pip install watchdog numpy matplotlib")
    sys.exit(1)


# ── Detection parameters (editable in GUI) ─────────────────
WINDOW = 10
CV_THRESH = 0.015
SLOPE_THRESH = 400
MIN_RECORDS = 25
CONSECUTIVE = 3
POLL_SEC = 2.0
ALERT_REPEAT = 60  # seconds between repeated beeps after trigger
# ───────────────────────────────────────────────────────────

PATTERN = b"Model 3321 Firmware"
CNT_OFFSET = 56
TS_OFFSET = 400  # 256 Hz tick counter


# ── Parsing ─────────────────────────────────────────────────


def parse_a21(path: Path):
    """Return list of (count, tick) tuples for every complete record."""
    try:
        data = path.read_bytes()
    except Exception:
        return []
    positions = [m.start() for m in re.finditer(re.escape(PATTERN), data)]
    out = []
    for p in positions:
        if p + TS_OFFSET + 4 > len(data):
            continue
        cnt = struct.unpack_from("<I", data, p + CNT_OFFSET)[0]
        tick = struct.unpack_from("<I", data, p + TS_OFFSET)[0]
        out.append((cnt, tick))
    return out


def ticks_to_elapsed(tick0, tick_i):
    """Convert tick difference to elapsed seconds (256 Hz counter)."""
    return (tick_i - tick0) / 256.0


def elapsed_to_str(sec):
    sec = int(sec)
    return f"{sec // 60:02d}:{sec % 60:02d}"


# ── Stability detection ─────────────────────────────────────


def rolling_stats(counts, idx, window):
    w = counts[max(0, idx - window + 1) : idx + 1]
    mean = np.mean(w)
    cv = np.std(w) / mean if mean > 0 else 999
    slope = np.polyfit(range(len(w)), w, 1)[0] if len(w) >= 2 else 0
    return cv, slope


def recompute_stability(counts, cv_t, sl_t, cons, min_rec):
    """Recompute per-record stability flags and find first trigger index."""
    n = len(counts)
    median_val = np.median(counts) if n >= min_rec else None
    cvs, slopes, flags = [], [], []

    for i in range(n):
        if i < min_rec - 1 or median_val is None:
            cvs.append(None)
            slopes.append(None)
            flags.append(False)
            continue
        cv, sl = rolling_stats(counts, i, WINDOW)
        above = counts[i] > median_val
        ok = cv < cv_t and abs(sl) < sl_t and above
        cvs.append(cv)
        slopes.append(sl)
        flags.append(ok)

    # Find first run of `cons` consecutive True flags
    trigger_idx = None
    streak = 0
    for i, ok in enumerate(flags):
        if ok:
            streak += 1
            if streak >= cons:
                trigger_idx = i
                break
        else:
            streak = 0

    return cvs, slopes, flags, trigger_idx


# ── File watcher ────────────────────────────────────────────


class A21Watcher:
    def __init__(self, q: queue.Queue):
        self.q = q
        self.mode = None
        self.target: Path | None = None
        self.folder: Path | None = None
        self._obs = None
        self._stop = threading.Event()
        self._last_count = 0
        self._last_mtime = 0.0

    def start_file(self, path: Path):
        self.mode = "file"
        self.target = path
        self._restart()

    def start_folder(self, folder: Path):
        self.mode = "folder"
        self.folder = folder
        self.target = self._newest(folder)
        self._restart()

    def stop(self):
        self._stop.set()
        if self._obs:
            self._obs.stop()
            self._obs.join()

    def _newest(self, folder: Path):
        files = list(folder.glob("*.A21")) + list(folder.glob("*.a21"))
        return max(files, key=lambda p: p.stat().st_mtime) if files else None

    def _restart(self):
        self.stop()
        self._stop.clear()
        self._last_count = 0
        self._last_mtime = 0.0
        watch_dir = self.folder if self.mode == "folder" else self.target.parent
        handler = _FsHandler(self)
        self._obs = Observer()
        self._obs.schedule(handler, str(watch_dir), recursive=False)
        self._obs.start()
        threading.Thread(target=self._poll_loop, daemon=True).start()
        self._check()

    def _poll_loop(self):
        while not self._stop.wait(POLL_SEC):
            self._check()

    def _check(self):
        if self.mode == "folder" and self.folder:
            newest = self._newest(self.folder)
            if newest and newest != self.target:
                self.target = newest
                self._last_count = 0
                self._last_mtime = 0.0
                self.q.put(("new_file", str(newest)))

        if not self.target or not self.target.exists():
            return
        try:
            mtime = self.target.stat().st_mtime
        except Exception:
            return
        if mtime == self._last_mtime:
            return
        self._last_mtime = mtime
        records = parse_a21(self.target)
        if len(records) != self._last_count:
            self._last_count = len(records)
            self.q.put(("data", records, str(self.target)))

    def notify(self):
        self._check()


class _FsHandler(FileSystemEventHandler):
    def __init__(self, w):
        self.w = w

    def on_modified(self, e):
        if not e.is_directory:
            self.w.notify()

    def on_created(self, e):
        if not e.is_directory:
            self.w.notify()


# ── Main GUI ─────────────────────────────────────────────────

BG = "#1e1e2e"
PANEL = "#2a2a3e"
ACC = "#7f77dd"
FG = "#cdd6f4"
TEAL = "#1d9e75"
AMB = "#ef9f27"
BLUE = "#85b7eb"
RED = "#ef4444"
FONT = ("Segoe UI", 10)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("APS A21 Real-Time Monitor")
        self.geometry("1060x720")
        self.configure(bg=BG)

        self.q = queue.Queue()
        self.watcher = A21Watcher(self.q)

        # State
        self.records: list[tuple] = []  # (count, tick)
        self.tick0: int | None = None  # tick value of rec 0 (256 Hz counter)
        self.triggered = False
        self.trigger_idx: int | None = None
        self.alert_thread: threading.Thread | None = None
        self.alert_stop = threading.Event()

        self._build_ui()
        self.after(250, self._poll_queue)

    # ── Layout ──────────────────────────────────────────────

    def _build_ui(self):
        # Top bar
        top = tk.Frame(self, bg=PANEL, pady=6)
        top.pack(fill="x")

        def btn(parent, text, cmd, color=ACC):
            return tk.Button(
                parent,
                text=text,
                command=cmd,
                bg=color,
                fg="white",
                relief="flat",
                padx=10,
                font=FONT,
                cursor="hand2",
            )

        btn(top, "Open file", self._pick_file).pack(side="left", padx=(10, 4))
        btn(top, "Watch folder", self._pick_folder, color="#534AB7").pack(
            side="left", padx=4
        )

        self.lbl_file = tk.Label(
            top, text="No file selected", bg=PANEL, fg="#666", font=FONT, anchor="w"
        )
        self.lbl_file.pack(side="left", padx=10, fill="x", expand=True)

        # Param row
        def param(parent, label, var, width=6):
            tk.Label(parent, text=label, bg=PANEL, fg=FG, font=FONT).pack(
                side="left", padx=(6, 2)
            )
            tk.Entry(
                parent,
                textvariable=var,
                width=width,
                bg="#3a3a5e",
                fg=FG,
                insertbackground=FG,
                relief="flat",
                font=FONT,
            ).pack(side="left")

        self.var_cv = tk.StringVar(value=str(CV_THRESH))
        self.var_slope = tk.StringVar(value=str(SLOPE_THRESH))
        self.var_consec = tk.StringVar(value=str(CONSECUTIVE))

        prow = tk.Frame(self, bg=PANEL, pady=3)
        prow.pack(fill="x")
        param(prow, "CV <", self.var_cv)
        param(prow, " |slope| <", self.var_slope)
        param(prow, " consec", self.var_consec, width=4)
        tk.Label(
            prow,
            text="  (parameters apply on next update)",
            bg=PANEL,
            fg="#555",
            font=("Segoe UI", 9),
        ).pack(side="left")

        # Stat row
        stat = tk.Frame(self, bg=PANEL, pady=4)
        stat.pack(fill="x")

        def stat_cell(parent, label):
            f = tk.Frame(parent, bg=PANEL)
            f.pack(side="left", padx=14)
            tk.Label(f, text=label, bg=PANEL, fg="#666", font=("Segoe UI", 9)).pack()
            lbl = tk.Label(f, text="—", bg=PANEL, fg=FG, font=("Segoe UI", 13, "bold"))
            lbl.pack()
            return lbl

        self.s_rec = stat_cell(stat, "Record #")
        self.s_time = stat_cell(stat, "Elapsed")
        self.s_val = stat_cell(stat, "Count (+56)")
        self.s_cv = stat_cell(stat, "CV")
        self.s_slope = stat_cell(stat, "Slope")
        self.s_streak = stat_cell(stat, "Streak")
        self.s_status = stat_cell(stat, "Status")

        # Chart
        cf = tk.Frame(self, bg=BG)
        cf.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        self.fig = Figure(figsize=(12, 6.2), facecolor=BG)
        self.fig.subplots_adjust(
            hspace=0.38, left=0.07, right=0.93, top=0.93, bottom=0.08
        )
        self.ax1 = self.fig.add_subplot(3, 1, (1, 2))
        self.ax2 = self.fig.add_subplot(3, 1, 3)
        for ax in (self.ax1, self.ax2):
            ax.set_facecolor("#11111e")
            ax.tick_params(colors="#888", labelsize=9)
            for sp in ax.spines.values():
                sp.set_color("#333")

        self.canvas = FigureCanvasTkAgg(self.fig, master=cf)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── File selection ───────────────────────────────────────

    def _pick_file(self):
        p = filedialog.askopenfilename(
            title="Select A21 file",
            filetypes=[("APS Data", "*.A21 *.a21"), ("All files", "*.*")],
        )
        if p:
            self._reset()
            path = Path(p)
            self.lbl_file.config(text=str(path), fg=FG)
            self.watcher.start_file(path)

    def _pick_folder(self):
        f = filedialog.askdirectory(title="Select folder to watch")
        if f:
            self._reset()
            path = Path(f)
            self.lbl_file.config(text=f"[folder]  {path}", fg="#9fe1cb")
            self.watcher.start_folder(path)

    def _reset(self):
        self.records = []
        self.tick0 = None
        self.triggered = False
        self.trigger_idx = None
        self.alert_stop.set()  # stop any running alert

    # ── Queue ────────────────────────────────────────────────

    def _poll_queue(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "data":
                    _, records, fpath = msg
                    self._on_data(records, fpath)
                elif msg[0] == "new_file":
                    self.lbl_file.config(
                        text=f"[folder -> new file]  {msg[1]}", fg="#9fe1cb"
                    )
                    self._reset()
        except queue.Empty:
            pass
        self.after(250, self._poll_queue)

    # ── Data update ──────────────────────────────────────────

    def _on_data(self, records, fpath):
        if not records:
            return
        self.records = records
        n = len(records)

        # Anchor to first record's tick — works correctly whether the file
        # already existed at startup or is being written in real time.
        # All elapsed times are derived purely from the instrument's 256 Hz
        # tick counter, so the wall-clock time this code runs is irrelevant.
        if self.tick0 is None:
            self.tick0 = records[0][1]

        # Build plain count list and elapsed times
        counts = [r[0] for r in records]
        elapsed = [ticks_to_elapsed(self.tick0, r[1]) for r in records]

        # Params
        try:
            cv_t = float(self.var_cv.get())
            sl_t = float(self.var_slope.get())
            cons = int(self.var_consec.get())
        except ValueError:
            cv_t, sl_t, cons = CV_THRESH, SLOPE_THRESH, CONSECUTIVE

        cvs, slopes, flags, trig_idx = recompute_stability(
            counts, cv_t, sl_t, cons, MIN_RECORDS
        )

        # Fire notification once
        if trig_idx is not None and not self.triggered:
            self.triggered = True
            self.trigger_idx = trig_idx
            self._start_alert(
                trig_idx + 1, counts[trig_idx], cvs[trig_idx], elapsed[trig_idx]
            )

        # Stat bar
        cv_now = next((c for c in reversed(cvs) if c is not None), None)
        sl_now = next((s for s in reversed(slopes) if s is not None), None)
        streak = 0
        for ok in reversed(flags):
            if ok:
                streak += 1
            else:
                break

        self.s_rec.config(text=str(n))
        self.s_time.config(text=elapsed_to_str(elapsed[-1]))
        self.s_val.config(text=f"{counts[-1]:,}")
        self.s_cv.config(
            text=f"{cv_now:.4f}" if cv_now is not None else "—",
            fg=TEAL if (cv_now and cv_now < cv_t) else RED if cv_now else "#888",
        )
        self.s_slope.config(
            text=f"{sl_now:+.0f}" if sl_now is not None else "—",
            fg=TEAL if (sl_now and abs(sl_now) < sl_t) else RED if sl_now else "#888",
        )
        self.s_streak.config(text=str(streak))
        if self.triggered:
            self.s_status.config(text=f"STABLE  rec {self.trigger_idx + 1}", fg=TEAL)
        elif n < MIN_RECORDS:
            self.s_status.config(text="Warming up", fg="#888")
        else:
            self.s_status.config(text="Rising", fg=AMB)

        self._redraw(counts, elapsed, cvs, slopes, flags, cv_t, sl_t)

    # ── Chart ────────────────────────────────────────────────

    def _redraw(self, counts, elapsed, cvs, slopes, flags, cv_t, sl_t):
        n = len(counts)
        xs = list(range(1, n + 1))  # record numbers (1-based)
        xmax = max(n + 1, 10)

        # ── Axis 1: count curve ──
        ax = self.ax1
        ax.cla()
        ax.set_facecolor("#11111e")
        ax.tick_params(colors="#888", labelsize=9)
        for sp in ax.spines.values():
            sp.set_color("#333")
        ax.set_ylabel("count (offset +56)", color="#888", fontsize=9)
        ax.set_title("Particle count — real time", color=FG, fontsize=11, pad=8)

        # Dual x-axis: top = elapsed time labels
        ax.set_xlim(0.5, xmax)
        ax2_top = ax.twiny()
        ax2_top.set_xlim(0.5, xmax)
        ax2_top.set_facecolor("#11111e")
        ax2_top.tick_params(colors="#888", labelsize=8)
        for sp in ax2_top.spines.values():
            sp.set_color("#333")
        # Label every ~10 records with elapsed time
        # Sample period derived from tick difference (exact, avoids hardcoding 10s)
        sample_sec = (
            ticks_to_elapsed(self.records[0][1], self.records[1][1])
            if len(self.records) >= 2
            else 10.0
        )

        tick_step = max(1, n // 10)
        top_ticks = list(range(1, n + 1, tick_step))
        # elapsed[i-1] is the START of record i; +sample_sec gives the END
        top_labels = [elapsed_to_str(elapsed[i - 1] + sample_sec) for i in top_ticks]
        ax2_top.set_xticks(top_ticks)
        ax2_top.set_xticklabels(top_labels, fontsize=8, color="#888")
        ax2_top.set_xlabel("Elapsed (mm:ss)", color="#888", fontsize=9, labelpad=2)

        # Stable region shading
        if self.trigger_idx is not None:
            ax.axvspan(self.trigger_idx + 1, xmax, alpha=0.08, color=TEAL, lw=0)
            ax.axvline(self.trigger_idx + 1, color=TEAL, lw=1.2, ls="--", alpha=0.8)

        # Median line
        if len(counts) >= MIN_RECORDS:
            med = np.median(counts)
            ax.axhline(med, color="#534AB7", lw=0.8, ls=":", alpha=0.6, label="median")

        # Main line
        ax.plot(xs, counts, color=ACC, lw=1.5, zorder=3)

        # Stable dots
        sx = [xs[i] for i, ok in enumerate(flags) if ok]
        sy = [counts[i] for i, ok in enumerate(flags) if ok]
        if sx:
            ax.scatter(sx, sy, color=TEAL, s=16, zorder=4, label="stable condition met")

        # Trigger star
        if self.trigger_idx is not None:
            ti = self.trigger_idx
            ax.scatter(
                [xs[ti]],
                [counts[ti]],
                color=AMB,
                s=90,
                marker="*",
                zorder=5,
                label=f"triggered  rec {ti + 1}  {elapsed_to_str(elapsed[ti])}",
            )
            ax.annotate(
                f"STABLE\nrec {ti + 1}\n{elapsed_to_str(elapsed[ti])}",
                xy=(xs[ti], counts[ti]),
                xytext=(min(xs[ti] + 3, n), counts[ti]),
                color=AMB,
                fontsize=8,
                arrowprops=dict(arrowstyle="->", color=AMB, lw=0.8),
            )

        ax.set_xlabel("Record #", color="#888", fontsize=9)
        ax.legend(
            fontsize=8, facecolor=BG, labelcolor=FG, framealpha=0.7, loc="upper left"
        )

        # ── Axis 2: CV + slope ──
        ax2 = self.ax2
        ax2.cla()
        ax2.set_facecolor("#11111e")
        ax2.tick_params(colors="#888", labelsize=9)
        for sp in ax2.spines.values():
            sp.set_color("#333")
        ax2.set_xlabel("Record #", color="#888", fontsize=9)
        ax2.set_ylabel("CV", color=BLUE, fontsize=9)
        ax2.set_xlim(0.5, xmax)

        vx = [xs[i] for i, c in enumerate(cvs) if c is not None]
        vcv = [c for c in cvs if c is not None]
        vsl = [slopes[i] for i, c in enumerate(cvs) if c is not None]

        if vx:
            ax2.plot(vx, vcv, color=BLUE, lw=1.2, label="CV")
            ax2.axhline(
                cv_t, color=BLUE, lw=0.7, ls="--", alpha=0.55, label=f"CV thresh {cv_t}"
            )
            ax2.tick_params(axis="y", colors=BLUE)

            ax2r = ax2.twinx()
            ax2r.set_facecolor("#11111e")
            ax2r.tick_params(colors=AMB, labelsize=9)
            for sp in ax2r.spines.values():
                sp.set_color("#333")
            # Plot slope in units of k-counts/record for readability
            sl_k = [s / 1000 for s in vsl]
            sl_t_k = sl_t / 1000
            ax2r.plot(vx, sl_k, color=AMB, lw=1.0, alpha=0.75, label="slope (k/rec)")
            ax2r.axhline(sl_t_k, color=AMB, lw=0.6, ls="--", alpha=0.45)
            ax2r.axhline(-sl_t_k, color=AMB, lw=0.6, ls="--", alpha=0.45)
            ax2r.axhline(0, color="#444", lw=0.5, alpha=0.5)
            ax2r.set_ylabel("slope  (k counts/rec)", color=AMB, fontsize=8)

        if self.trigger_idx is not None:
            ax2.axvline(self.trigger_idx + 1, color=TEAL, lw=1.2, ls="--", alpha=0.8)

        ax2.legend(
            fontsize=8, facecolor=BG, labelcolor=FG, framealpha=0.7, loc="upper left"
        )

        self.canvas.draw_idle()

    # ── Alert ────────────────────────────────────────────────

    def _start_alert(self, rec, val, cv, elapsed_sec):
        self.alert_stop.clear()

        def _alert_loop():
            msg = (
                f"Particle count has reached a stable plateau.\n\n"
                f"Record:   {rec}\n"
                f"Elapsed:  {elapsed_to_str(elapsed_sec)}\n"
                f"Count:    {val:,}\n"
                f"CV:       {cv:.4f}\n\n"
                f"Click OK to acknowledge and stop the alert."
            )

            while not self.alert_stop.is_set():
                # Sound: 3 beeps
                try:
                    import winsound

                    for _ in range(3):
                        winsound.Beep(1000, 300)
                        time.sleep(0.15)
                except Exception:
                    pass  # non-Windows: silent

                # Blocking dialog — waits for OK
                # Run in main thread via after() to be tk-safe
                ack_evt = threading.Event()

                def _show():
                    messagebox.showinfo("APS Monitor — Stable!", msg)
                    ack_evt.set()
                    self.alert_stop.set()  # stop repeating once acknowledged

                self.after(0, _show)
                ack_evt.wait()  # block until dialog closes

                if not self.alert_stop.is_set():
                    # User closed without clicking — wait then repeat
                    self.alert_stop.wait(ALERT_REPEAT)

        self.alert_thread = threading.Thread(target=_alert_loop, daemon=True)
        self.alert_thread.start()

    def _on_close(self):
        self.alert_stop.set()
        self.watcher.stop()
        self.destroy()


# ── Entry point ─────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    if len(sys.argv) > 1:
        p = Path(sys.argv[1])
        if p.is_dir():
            app.watcher.start_folder(p)
            app.lbl_file.config(text=f"[folder]  {p}", fg="#9fe1cb")
        elif p.exists():
            app.watcher.start_file(p)
            app.lbl_file.config(text=str(p), fg=FG)
    app.mainloop()
