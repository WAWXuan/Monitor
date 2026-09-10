# APS A21 Real-Time Monitor

A small desktop GUI that watches a TSI APS Model 3321 `.A21` file while an
experiment is running. It plots the particle count stored at record offset
`+56`, calculates rolling stability metrics, and alerts when the signal reaches
a stable plateau.

## Features

- Watch one `.A21` file or automatically follow the newest `.A21` file in a folder.
- Plot particle count, coefficient of variation (CV), and slope in real time.
- Derive elapsed time from the instrument's 256 Hz tick counter.
- Adjust the CV, slope, and consecutive-record thresholds in the GUI.
- Show a repeating acknowledgement dialog after stability is detected.
- Play an audible beep on Windows when stability is detected.

## Requirements

- Python 3.13 or newer
- [`uv`](https://docs.astral.sh/uv/)
- Tkinter (included with most standard Python installations)

## Run

Clone the repository, enter its directory, and run:

```bash
uv sync
uv run python a21_monitor_gui.py
```

You can also open a file or folder directly:

```bash
uv run python a21_monitor_gui.py /path/to/experiment.A21
uv run python a21_monitor_gui.py /path/to/experiment-folder
```

Without a path, use **Open file** or **Watch folder** in the GUI.

## Stability rule

After at least 25 records, the monitor evaluates a rolling 10-record window.
A record is considered stable when all of the following are true:

- CV is below `0.015`.
- Absolute slope is below `400` counts per record.
- The current count is above the median of the loaded records.

The alert triggers after three consecutive stable records. These thresholds can
be changed in the GUI; the window size and minimum record count are constants in
`a21_monitor_gui.py`.

## Distribution

This repository contains Python source code, not a prebuilt standalone
executable. Run it with `uv` as shown above. A platform-specific executable can
be built separately with a packaging tool such as PyInstaller if needed.
