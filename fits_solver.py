# =============================================================================
# FITS Calibrator — Automatic Platesolver & Calibration for NINA Imaging Sessions
#
# PURPOSE:
#   Monitors a watch directory for new FITS files as they arrive from the
#   camera. Each new file is optionally calibrated (dark, flat, bias) and/or
#   run through a platesolving engine (e.g. ASTAP).
#   Calibrated files  → Output/Calibrated/
#   Platesolved files → Output/Platesolved/
#
# INSTALLATION:
#   pip install watchdog ccdproc astropy numpy
#   Install ASTAP (or other solver): https://www.hnsky.org/astap.htm
#
# RUN:
#   python fits_solver.py
#
# FILES CREATED ALONGSIDE THIS SCRIPT:
#   fits_calibrator_settings.json  — saved settings, restored on next launch
#   fits_calibrator_Log.log        — full activity log with timestamps
# =============================================================================

import sys
import time
import os
import json
import shutil
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime, timedelta
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

try:
    import numpy as np
    from astropy.io import fits as astropy_fits
    from astropy.nddata import CCDData
    import astropy.units as u
    from ccdproc import combine, subtract_bias, subtract_dark, flat_correct
    _CCDPROC_OK = True
except ImportError:
    _CCDPROC_OK = False

VERSION = "2.2.0"
BUILD   = "20260328"

# ── Color palette (dark theme — matches EXOTIC Launcher) ──────────────────────
C_BG      = "#2e3440"   # main window background
C_BG2     = "#3b4252"   # panels / header bg
C_FIELD   = "#434c5e"   # entry / combobox field background
C_FG      = "#eceff4"   # primary text
C_ACCENT  = "#88c0d0"   # section headers (soft cyan)
C_HINT    = "#8892a0"   # secondary / hint text
C_BTN     = "#5e81ac"   # button background (steel blue)
C_BTN_ACT = "#81a1c1"   # button on hover
C_SEP     = "#4c566a"   # separator / section bar
C_WARN    = "#ebcb8b"   # warning text (amber)
C_OK      = "#a3be8c"   # success text (green)
C_ERR     = "#bf616a"   # error text (red)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

if getattr(sys, "frozen", False):
    _RUNTIME_DIR = Path(sys.executable).parent
    _BUNDLE_DIR  = Path(sys._MEIPASS)
else:
    _RUNTIME_DIR = Path(__file__).parent
    _BUNDLE_DIR  = Path(__file__).parent

SETTINGS_FILE   = _RUNTIME_DIR / "fits_calibrator_settings.json"
LOG_FILE        = _RUNTIME_DIR / "fits_calibrator_Log.log"
FITS_EXTENSIONS = {".fits", ".fit", ".fts"}

# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_settings(data: dict):
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Could not save settings: {e}")

def prune_log(log_path: Path, keep_sessions: int = 3):
    if not log_path.exists():
        return
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines(keepends=True)
    except Exception:
        return
    markers = [i for i, line in enumerate(lines) if "Monitoring STARTED" in line]
    if len(markers) <= keep_sessions:
        return
    cut_from = markers[-keep_sessions]
    try:
        log_path.write_text("".join(lines[cut_from:]), encoding="utf-8")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

class Logger:
    """Thread-safe logger that posts to the GUI text widget and a log file."""

    def __init__(self, log_widget: tk.Text, log_path: Path):
        self.widget       = log_widget
        self.error_widget = None
        self.log_path     = log_path
        self._lock        = threading.Lock()

    def set_error_widget(self, widget: tk.Text):
        self.error_widget = widget

    def _timestamp(self):
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def log(self, level: str, msg: str) -> str:
        line = f"[{self._timestamp()}]  {level:<8}  {msg}"
        with self._lock:
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass
        if self.widget:
            self.widget.after(0, self._append_to_widget, line)
        return line

    def _append_to_widget(self, line: str):
        self.widget.config(state="normal")
        self.widget.insert("end", line + "\n")
        self.widget.see("end")
        self.widget.config(state="disabled")

    def _append_to_error_widget(self, line: str, tag: str):
        self.error_widget.config(state="normal")
        self.error_widget.insert("end", line + "\n", tag)
        self.error_widget.see("end")
        self.error_widget.config(state="disabled")

    def info   (self, msg): self.log("INFO",    msg)
    def success(self, msg): self.log("SUCCESS", msg)
    def detail (self, msg): self.log("  ·",     msg)

    def warn(self, msg):
        line = self.log("WARNING", msg)
        if self.error_widget:
            self.error_widget.after(0, self._append_to_error_widget, line, "warning")

    def error(self, msg):
        line = self.log("ERROR", msg)
        if self.error_widget:
            self.error_widget.after(0, self._append_to_error_widget, line, "error")

# ---------------------------------------------------------------------------
# Toggle switch widget  (iOS-style)
# ---------------------------------------------------------------------------

class ToggleSwitch(tk.Canvas):

    def __init__(self, parent, variable, on_color=C_OK, off_color=None, **kwargs):
        try:
            bg = parent.cget("bg")
        except Exception:
            bg = C_BG
        super().__init__(parent, width=46, height=22, bg=bg,
                         highlightthickness=0, cursor="hand2", **kwargs)
        self._var       = variable
        self._on_color  = on_color
        self._off_color = off_color if off_color is not None else C_HINT
        self._enabled   = True
        self.bind("<Button-1>", self._toggle)
        self._var.trace_add("write", lambda *_: self._redraw())
        self._redraw()

    def _toggle(self, _=None):
        if self._enabled:
            self._var.set(not self._var.get())

    def set_enabled(self, enabled: bool):
        self._enabled = enabled
        if enabled:
            self.bind("<Button-1>", self._toggle)
            self.config(cursor="hand2")
        else:
            self.unbind("<Button-1>")
            self.config(cursor="")
        self._redraw()

    def _redraw(self):
        self.delete("all")
        on  = self._var.get()
        if not self._enabled:
            clr = C_SEP
        else:
            clr = self._on_color if on else self._off_color
        w, h = 46, 22
        self.create_oval(0, 0, h, h, fill=clr, outline="")
        self.create_oval(w - h, 0, w, h, fill=clr, outline="")
        self.create_rectangle(h // 2, 0, w - h // 2, h, fill=clr, outline="")
        kx = w - h // 2 - 1 if on else h // 2 + 1
        self.create_oval(kx - 9, 1, kx + 9, 21, fill=C_FG, outline=C_SEP)

# ---------------------------------------------------------------------------
# File stability check
# ---------------------------------------------------------------------------

STABILITY_INTERVAL = 3.0
STABILITY_CHECKS   = 3
STABILITY_TIMEOUT  = 120

def wait_for_stable(filepath: Path) -> bool:
    last_size  = -1
    stable_cnt = 0
    elapsed    = 0.0
    while elapsed < STABILITY_TIMEOUT:
        try:
            size = filepath.stat().st_size
        except OSError:
            size = -1
        if size == last_size and size >= 0:
            stable_cnt += 1
            if stable_cnt >= STABILITY_CHECKS:
                return True
        else:
            stable_cnt = 0
        last_size = size
        time.sleep(STABILITY_INTERVAL)
        elapsed  += STABILITY_INTERVAL
    return False

# ---------------------------------------------------------------------------
# Calibration helpers (science mode)
# ---------------------------------------------------------------------------

def build_master(folder: Path, frame_type: str, logger):
    """Median-combine FITS frames with sigma clipping. Used by science mode."""
    if not _CCDPROC_OK:
        return None
    fits_files = [f for f in folder.iterdir()
                  if f.suffix.lower() in FITS_EXTENSIONS]
    if not fits_files:
        logger.warn(f"No FITS files found in {frame_type} folder: {folder}")
        return None
    logger.info(f"Building {frame_type} master from {len(fits_files)} frames …")
    frames = []
    for fpath in fits_files:
        try:
            frames.append(CCDData.read(str(fpath), unit=u.adu))
        except Exception as e:
            logger.warn(f"  Skipping {fpath.name}: {e}")
    if not frames:
        logger.error(f"Could not load any {frame_type} frames.")
        return None
    master = combine(frames, method="median",
                     sigma_clip=True,
                     sigma_clip_low_thresh=5,
                     sigma_clip_high_thresh=5,
                     sigma_clip_func=np.ma.median)
    logger.success(f"{frame_type} master built ({len(frames)} frames).")
    return master

def calibrate_file(filepath: Path, output_dir: Path,
                   master_dark, master_flat, master_bias,
                   logger):
    if not _CCDPROC_OK:
        return None
    try:
        with astropy_fits.open(str(filepath)) as hdul:
            orig_data = None
            orig_header = None
            for hdu in hdul:
                if hdu.data is not None and hdu.data.ndim == 2:
                    orig_data   = hdu.data.copy()
                    orig_header = hdu.header.copy()
                    break
            if orig_data is None:
                orig_data   = hdul[0].data.copy()
                orig_header = hdul[0].header.copy()
            if orig_data is None:
                orig_header = hdul[1].header.copy()
        light = CCDData(orig_data.astype(np.float32), header=orig_header, unit=u.adu)
    except Exception as e:
        logger.error(f"Cannot read {filepath.name}: {e}")
        return None

    try:
        if master_bias is not None:
            light = subtract_bias(light, master_bias)
        if master_dark is not None:
            light = subtract_dark(light, master_dark,
                                  dark_exposure=1*u.s, data_exposure=1*u.s,
                                  scale=True)
        if master_flat is not None:
            light = flat_correct(light, master_flat)
    except Exception as e:
        logger.error(f"Calibration failed for {filepath.name}: {e}")
        return None

    stem = filepath.stem
    out_name = stem + "_CAL.fits"
    cal_dir  = output_dir / "Calibrated"
    cal_dir.mkdir(parents=True, exist_ok=True)
    out_path = cal_dir / out_name

    try:
        data_arr  = light.data.astype(np.float32)
        hdr       = light.header.copy()
        primary   = astropy_fits.PrimaryHDU(data=data_arr, header=hdr)
        hdul_out  = astropy_fits.HDUList([primary])
        hdul_out.writeto(str(out_path), overwrite=True)
    except Exception as e:
        logger.error(f"Cannot write calibrated file {out_name}: {e}")
        return None

    logger.success(f"Calibrated → {out_name}")
    return out_path

def platesolve_file(filepath: Path, output_dir: Path,
                    solver_exe: str, search_radius: float,
                    logger) -> bool:
    cal_dir = output_dir / "Calibrated"
    cal_dir.mkdir(parents=True, exist_ok=True)

    if filepath.parent.resolve() == cal_dir.resolve():
        work_path = filepath
        stem      = filepath.stem
        out_name  = stem + "_WCS.fits"
        out_path  = cal_dir / out_name
    else:
        stem     = filepath.stem
        out_name = stem + "_WCS.fits"
        out_path = cal_dir / out_name
        try:
            shutil.copy2(str(filepath), str(out_path))
        except Exception as e:
            logger.error(f"Cannot copy {filepath.name} for platesolving: {e}")
            return False
        work_path = out_path

    cmd = [solver_exe, "-f", str(work_path),
           "-r", str(search_radius), "-update"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        logger.error(f"Solver not found: {solver_exe}")
        return False
    except subprocess.TimeoutExpired:
        logger.error(f"Solver timed out on {work_path.name}")
        return False
    except Exception as e:
        logger.error(f"Solver error on {work_path.name}: {e}")
        return False

    ini_path = work_path.with_suffix(".ini")
    wcs_path = work_path.with_suffix(".wcs")
    solved   = False
    if ini_path.exists():
        try:
            content = ini_path.read_text(encoding="utf-8", errors="replace")
            solved  = "PLTSOLVD=T" in content
        except Exception:
            pass
        try:
            ini_path.unlink()
        except Exception:
            pass
    if wcs_path.exists():
        try:
            wcs_path.unlink()
        except Exception:
            pass

    if solved:
        logger.success(f"Platesolved → {work_path.name}")
    else:
        logger.warn(f"Platesolve FAILED for {work_path.name}")
        if result.stderr:
            logger.detail(f"  stderr: {result.stderr[:200]}")

    return solved

# ---------------------------------------------------------------------------
# Build and save master — Create Master Cal Files mode
# ---------------------------------------------------------------------------

def build_and_save_master(input_dir: Path, output_dir: Path,
                           frame_type: str, sigma: float,
                           logger, cal_counter_updater,
                           cancel_event: threading.Event) -> bool:
    """
    Loads all FITS files from input_dir, combines with sigma-clipped median,
    and saves to output_dir as master_dark.fits / master_flat.fits / master_bias.fits.
    Returns True on success, False on failure or cancellation.
    """
    if not _CCDPROC_OK:
        logger.error("ccdproc/astropy not installed — cannot build master.")
        if cal_counter_updater:
            cal_counter_updater(0, 1, 1)
        return False

    if cal_counter_updater:
        cal_counter_updater(0, 0, 1)

    fits_files = [f for f in input_dir.iterdir()
                  if f.suffix.lower() in FITS_EXTENSIONS]
    if not fits_files:
        logger.error(f"No FITS files found in: {input_dir}")
        if cal_counter_updater:
            cal_counter_updater(0, 1, 1)
        return False

    logger.info(f"Building master {frame_type} from {len(fits_files)} frames "
                f"(sigma={sigma}) …")
    frames = []
    for fpath in fits_files:
        if cancel_event and cancel_event.is_set():
            logger.warn("Master build cancelled during frame load.")
            if cal_counter_updater:
                cal_counter_updater(0, 1, 1)
            return False
        try:
            frames.append(CCDData.read(str(fpath), unit=u.adu))
        except Exception as e:
            logger.warn(f"  Skipping {fpath.name}: {e}")

    if not frames:
        logger.error(f"Could not load any {frame_type} frames.")
        if cal_counter_updater:
            cal_counter_updater(0, 1, 1)
        return False

    # Build filename suffix from first frame's FITS header
    _hdr0 = frames[0].header
    _date_raw = _hdr0.get("DATE-OBS", "")
    _date     = str(_date_raw)[:10] if _date_raw else ""
    _filter   = str(_hdr0.get("FILTER", "")).strip()
    _temp     = str(_hdr0.get("CCD-TEMP", "")).strip()
    _exp_raw  = _hdr0.get("EXPOSURE", "")
    try:
        _exp = f"{float(_exp_raw):.2f}" if _exp_raw != "" else ""
    except (ValueError, TypeError):
        _exp = ""
    _gain  = str(_hdr0.get("GAIN", "")).strip()
    _xbin  = str(_hdr0.get("XBINNING", "")).strip()
    _ybin  = str(_hdr0.get("YBINNING", "")).strip()
    _bin   = f"{_xbin}x{_ybin}" if _xbin and _ybin else (_xbin or _ybin)
    _parts = [p for p in [_date, _filter, _temp, _exp, _gain, _bin] if p]
    _suffix = ("_" + "_".join(_parts)) if _parts else ""

    if cancel_event and cancel_event.is_set():
        logger.warn("Master build cancelled before combine.")
        if cal_counter_updater:
            cal_counter_updater(0, 1, 1)
        return False

    try:
        master = combine(frames, method="median",
                         sigma_clip=True,
                         sigma_clip_low_thresh=sigma,
                         sigma_clip_high_thresh=sigma,
                         sigma_clip_func=np.ma.median)
    except Exception as e:
        logger.error(f"Combine failed: {e}")
        if cal_counter_updater:
            cal_counter_updater(0, 1, 1)
        return False

    out_name = f"master_{frame_type}{_suffix}.fits"
    out_path = output_dir / out_name
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        data_arr = master.data.astype(np.float32)
        hdr      = master.header.copy() if master.header else astropy_fits.Header()
        primary  = astropy_fits.PrimaryHDU(data=data_arr, header=hdr)
        astropy_fits.HDUList([primary]).writeto(str(out_path), overwrite=True)
    except Exception as e:
        logger.error(f"Cannot write {out_name}: {e}")
        if cal_counter_updater:
            cal_counter_updater(0, 1, 1)
        return False

    logger.success(f"Master {frame_type} saved → {out_path}")
    if cal_counter_updater:
        cal_counter_updater(1, 0, 1)
    return True

# ---------------------------------------------------------------------------
# Watchdog handler
# ---------------------------------------------------------------------------

def _is_output_file(filepath: Path, output_dir: Path) -> bool:
    cal_dir = output_dir / "Calibrated"
    try:
        filepath.resolve().relative_to(cal_dir.resolve())
        return True
    except ValueError:
        return False

class FITSWatcher(FileSystemEventHandler):

    def __init__(self, watch_dir: Path, output_dir: Path,
                 solver_exe: str, search_radius: float,
                 logger,
                 platesolve_enabled: bool = True,
                 cal_enabled: bool = False,
                 darks_dir: Path | None = None,
                 flats_dir: Path | None = None,
                 bias_dir:  Path | None = None,
                 cal_source: str = "raw",
                 status_updater=None,
                 ps_counter_updater=None,
                 cal_counter_updater=None):
        super().__init__()
        self.watch_dir          = watch_dir
        self.output_dir         = output_dir
        self.solver_exe         = solver_exe
        self.search_radius      = search_radius
        self.logger             = logger
        self.platesolve_enabled = platesolve_enabled
        self.cal_enabled        = cal_enabled
        self.darks_dir          = darks_dir
        self.flats_dir          = flats_dir
        self.bias_dir           = bias_dir
        self.cal_source         = cal_source
        self.status_updater     = status_updater
        self.ps_counter_updater = ps_counter_updater
        self.cal_counter_updater = cal_counter_updater

        self._master_dark  = None
        self._master_flat  = None
        self._master_bias  = None
        self._cancelled    = threading.Event()
        self._processing   = threading.Lock()

        self._cal_solved  = 0
        self._cal_failed  = 0
        self._cal_total   = 0
        self._ps_solved   = 0
        self._ps_failed   = 0
        self._ps_total    = 0

    def cancel(self):
        self._cancelled.set()

    def build_masters(self):
        if not self.cal_enabled or not _CCDPROC_OK:
            return
        if self.cal_source == "master":
            if self.darks_dir and self.darks_dir.is_file():
                try:
                    self._master_dark = CCDData.read(str(self.darks_dir), unit=u.adu)
                    self.logger.success(f"Loaded master dark: {self.darks_dir.name}")
                except Exception as e:
                    self.logger.error(f"Cannot load master dark: {e}")
            if self.flats_dir and self.flats_dir.is_file():
                try:
                    self._master_flat = CCDData.read(str(self.flats_dir), unit=u.adu)
                    self.logger.success(f"Loaded master flat: {self.flats_dir.name}")
                except Exception as e:
                    self.logger.error(f"Cannot load master flat: {e}")
            if self.bias_dir and self.bias_dir.is_file():
                try:
                    self._master_bias = CCDData.read(str(self.bias_dir), unit=u.adu)
                    self.logger.success(f"Loaded master bias: {self.bias_dir.name}")
                except Exception as e:
                    self.logger.error(f"Cannot load master bias: {e}")
        else:
            if self.darks_dir:
                self._master_dark = build_master(self.darks_dir, "Dark", self.logger)
            if self.flats_dir:
                self._master_flat = build_master(self.flats_dir, "Flat", self.logger)
            if self.bias_dir:
                self._master_bias = build_master(self.bias_dir,  "Bias", self.logger)

    def scan_existing_files(self):
        existing = [
            f for f in self.watch_dir.rglob("*")
            if f.suffix.lower() in FITS_EXTENSIONS
            and f.is_file()
            and not _is_output_file(f, self.output_dir)
        ]
        self._cal_total += len(existing)
        self._ps_total  += len(existing)
        for fpath in existing:
            if self._cancelled.is_set():
                break
            self._process(fpath)
        if self.status_updater:
            self.status_updater("● Watching", C_ACCENT)

    def on_created(self, event):
        if event.is_directory:
            return
        fpath = Path(event.src_path)
        if fpath.suffix.lower() not in FITS_EXTENSIONS:
            return
        if _is_output_file(fpath, self.output_dir):
            return
        if self._cancelled.is_set():
            return
        self._cal_total += 1
        self._ps_total  += 1
        if self.status_updater:
            self.status_updater("● Watching", C_ACCENT)
        threading.Thread(target=self._process_new, args=(fpath,), daemon=True).start()

    def _process_new(self, fpath: Path):
        if not wait_for_stable(fpath):
            self.logger.warn(f"File did not stabilise: {fpath.name}")
            return
        self._process(fpath)

    def _process(self, fpath: Path):
        with self._processing:
            if self._cancelled.is_set():
                return
            if self.status_updater:
                self.status_updater("⚙  Processing ...", C_ACCENT)

            cal_path = None
            if self.cal_enabled and _CCDPROC_OK:
                cal_path = calibrate_file(
                    fpath, self.output_dir,
                    self._master_dark, self._master_flat, self._master_bias,
                    self.logger)
                with threading.Lock():
                    if cal_path:
                        self._cal_solved += 1
                    else:
                        self._cal_failed += 1
                    s, f, t = self._cal_solved, self._cal_failed, self._cal_total
                if self.cal_counter_updater:
                    self.cal_counter_updater(s, f, t)

            if self._cancelled.is_set():
                if self.status_updater:
                    self.status_updater("● Watching", C_ACCENT)
                return

            if self.platesolve_enabled and self.solver_exe:
                solve_src = cal_path if cal_path else fpath
                if self.status_updater:
                    self.status_updater("⚙  Processing ...", C_ACCENT)
                ok = platesolve_file(
                    solve_src, self.output_dir,
                    self.solver_exe, self.search_radius,
                    self.logger)
                with threading.Lock():
                    if ok:
                        self._ps_solved += 1
                    else:
                        self._ps_failed += 1
                    s, f, t = self._ps_solved, self._ps_failed, self._ps_total
                if self.ps_counter_updater:
                    self.ps_counter_updater(s, f, t)

            if self.status_updater:
                self.status_updater("● Watching", C_ACCENT)

# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class App:

    def __init__(self, root: tk.Tk):
        self.root     = root
        self.root.title(f"FITS Calibrator  v{VERSION} · build {BUILD}")
        self.root.geometry("1360x1570")
        self.root.resizable(True, True)
        self.root.configure(bg=C_BG)

        # ── Scrollable canvas ─────────────────────────────────────────────────
        _vsb    = ttk.Scrollbar(root, orient="vertical")
        _canvas = tk.Canvas(root, highlightthickness=0, bg=C_BG,
                            yscrollcommand=_vsb.set)
        _vsb.config(command=_canvas.yview)
        _vsb.pack(side="right", fill="y")
        _canvas.pack(side="left", fill="both", expand=True)

        self._scroll_frame = tk.Frame(_canvas, bg=C_BG)
        _cw = _canvas.create_window((0, 0), window=self._scroll_frame, anchor="nw")

        def _on_frame_resize(event):
            _canvas.configure(scrollregion=_canvas.bbox("all"))
        self._scroll_frame.bind("<Configure>", _on_frame_resize)

        def _on_canvas_resize(event):
            _canvas.itemconfig(_cw, width=event.width)
        _canvas.bind("<Configure>", _on_canvas_resize)

        def _on_mousewheel(event):
            if not isinstance(event.widget, (tk.Text, tk.Listbox)):
                _canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        self.root.bind_all("<MouseWheel>", _on_mousewheel)

        # Rebind local 'root' so all widget-creation code below targets the
        # scroll frame automatically.
        root = self._scroll_frame

        self.observer              = None
        self.watcher               = None
        self._batch_watcher        = None
        self._schedule_cancel      = None
        self._stop_schedule_cancel = None
        self._master_cancel_event  = None
        self._master_type_updating = False

        s   = load_settings()
        pad = {"padx": 10, "pady": 4}

        # ── Header ───────────────────────────────────────────────────────────
        header = tk.Frame(root, bg=C_BG2, pady=9)
        header.grid(row=0, column=0, columnspan=3, sticky="ew")
        tk.Label(header, text="FITS Calibrator",
                 font=("Arial", 14, "bold"), bg=C_BG2, fg=C_FG).pack(side="left", padx=14)
        tk.Button(header, text="User Guide", font=("", 11),
                  bg=C_BTN, fg=C_FG, activebackground=C_BTN_ACT,
                  activeforeground=C_FG, relief="flat", padx=6, pady=2,
                  command=self._show_instructions).pack(side="right", padx=14)

        # ── Mode toggle bar ───────────────────────────────────────────────────
        self._app_mode = s.get("app_mode", "science")
        mode_bar = tk.Frame(root, bg=C_BG2, pady=4)
        mode_bar.grid(row=1, column=0, columnspan=3, sticky="ew", padx=6, pady=(0, 2))
        self._mode_btn_science = tk.Button(
            mode_bar, text="Calibrate Science Frames",
            font=("", 11, "bold"), relief="flat", padx=10, pady=3,
            command=lambda: self._on_mode_change("science"))
        self._mode_btn_science.pack(side="left", padx=(8, 2), pady=4)
        self._mode_btn_master = tk.Button(
            mode_bar, text="Create Master Cal Files",
            font=("", 11, "bold"), relief="flat", padx=10, pady=3,
            command=lambda: self._on_mode_change("master"))
        self._mode_btn_master.pack(side="left", padx=(2, 8), pady=4)

        # ── Directories ──────────────────────────────────────────────────────
        self._section(root, "Directories", row=2)

        self._input_dir_label = tk.Label(root, text="Input Directory:", bg=C_BG, fg=C_FG)
        self._input_dir_label.grid(row=3, column=0, sticky="w", **pad)
        _watch_default = s.get("watch_dir", "")
        self.watch_var = tk.StringVar(value=_watch_default)
        wf = tk.Frame(root, bg=C_BG); wf.grid(row=3, column=1, columnspan=2, padx=5, sticky="ew")
        wf.columnconfigure(0, weight=1)
        tk.Entry(wf, textvariable=self.watch_var,
                 bg=C_FIELD, fg=C_FG, insertbackground=C_FG,
                 relief="flat").grid(row=0, column=0, sticky="ew")
        tk.Button(wf, text="Browse", bg=C_BTN, fg=C_FG,
                  activebackground=C_BTN_ACT, relief="flat",
                  command=self._browse_watch).grid(row=0, column=1, padx=4)

        tk.Label(root, text="Output Directory:", bg=C_BG, fg=C_FG).grid(
            row=4, column=0, sticky="w", padx=10, pady=(14, 4))
        self.output_var = tk.StringVar(value=s.get("output_dir", _watch_default))
        of = tk.Frame(root, bg=C_BG)
        of.grid(row=4, column=1, columnspan=2, padx=5, pady=(14, 4), sticky="ew")
        of.columnconfigure(0, weight=1)
        tk.Entry(of, textvariable=self.output_var,
                 bg=C_FIELD, fg=C_FG, insertbackground=C_FG,
                 relief="flat").grid(row=0, column=0, sticky="ew")
        tk.Button(of, text="Browse", bg=C_BTN, fg=C_FG,
                  activebackground=C_BTN_ACT, relief="flat",
                  command=self._browse_output).grid(row=0, column=1, padx=4)

        # ── Calibration ───────────────────────────────────────────────────────
        self._cal_section_label = self._section(root, "Calibration", row=5)

        # Science mode: Enable toggle + Darks/Flats/Bias rows
        self._cal_toggle_row = tk.Frame(root, bg=C_BG)
        self._cal_toggle_row.grid(row=6, column=0, columnspan=3, sticky="w", padx=12, pady=(6, 2))

        # Line 1: Enable Calibration
        _cal_line1 = tk.Frame(self._cal_toggle_row, bg=C_BG)
        _cal_line1.pack(side="top", anchor="w", pady=(0, 4))
        self.cal_enabled_var = tk.BooleanVar(value=s.get("cal_enabled", False))
        ToggleSwitch(_cal_line1, self.cal_enabled_var, on_color=C_ACCENT).pack(side="left")
        tk.Label(_cal_line1, text="Enable Calibration",
                 font=("", 11), bg=C_BG, fg=C_FG).pack(side="left", padx=10)
        tk.Label(_cal_line1,
                 text="(A 'Calibrated' subdirectory will be created automatically)",
                 fg=C_HINT, font=("", 11), bg=C_BG).pack(side="left", padx=(0, 10))
        tk.Label(_cal_line1,
                 text="•  Algorithm: bias subtract → dark subtract (exposure-scaled) → flat correct",
                 fg=C_HINT, font=("", 11), bg=C_BG).pack(side="left", padx=(0, 10))

        # Line 2: Raw / Master source toggle (indented to align with line 1 text)
        _cal_line2 = tk.Frame(self._cal_toggle_row, bg=C_BG)
        _cal_line2.pack(side="top", anchor="w")
        tk.Label(_cal_line2, text="", width=3, bg=C_BG).pack(side="left")  # indent
        self.cal_use_master_var = tk.BooleanVar(value=s.get("cal_use_master", False))
        _cm_init = self.cal_use_master_var.get()
        self._cal_raw_lbl = tk.Label(_cal_line2, text="Use raw frames",
                                     font=("", 11), bg=C_BG,
                                     fg=C_HINT if _cm_init else C_ACCENT)
        self._cal_raw_lbl.pack(side="left", padx=(0, 6))
        ToggleSwitch(_cal_line2, self.cal_use_master_var,
                     on_color=C_WARN, off_color=C_ACCENT).pack(side="left", padx=(0, 4))
        self._cal_master_lbl = tk.Label(_cal_line2, text="Use master files",
                                        font=("", 11), bg=C_BG,
                                        fg=C_WARN if _cm_init else C_HINT)
        self._cal_master_lbl.pack(side="left")

        self.cal_enabled_var.trace_add("write", self._on_cal_toggle)
        self.cal_use_master_var.trace_add("write", self._on_cal_source_toggle)

        _use_master_init = s.get("cal_use_master", False)
        self._darks_dir_val    = s.get("darks_dir", "")
        self._darks_master_val = s.get("darks_master_file", "")
        self._flats_dir_val    = s.get("flats_dir", "")
        self._flats_master_val = s.get("flats_master_file", "")
        self._bias_dir_val     = s.get("bias_dir", "")
        self._bias_master_val  = s.get("bias_master_file", "")

        self._darks_row = tk.Frame(root, bg=C_BG)
        self._darks_row.grid(row=7, column=0, columnspan=3, sticky="ew", padx=20, pady=(8, 2))
        self._darks_row.columnconfigure(2, weight=1)
        self.darks_enabled_var = tk.BooleanVar(value=s.get("darks_enabled", True))
        self._darks_toggle = ToggleSwitch(self._darks_row, self.darks_enabled_var, on_color=C_ACCENT)
        self._darks_toggle.grid(row=0, column=0, padx=(0, 8))
        tk.Label(self._darks_row, text="Darks:", width=7, anchor="w",
                 bg=C_BG, fg=C_FG).grid(row=0, column=1)
        self.darks_var = tk.StringVar(value=self._darks_master_val if _use_master_init else self._darks_dir_val)
        self._darks_entry = tk.Entry(self._darks_row, textvariable=self.darks_var,
                                     bg=C_FIELD, fg=C_FG, insertbackground=C_FG, relief="flat")
        self._darks_entry.grid(row=0, column=2, sticky="ew", padx=(0, 4))
        self._darks_browse = tk.Button(self._darks_row, text="Browse",
                                       bg=C_BTN, fg=C_FG, activebackground=C_BTN_ACT, relief="flat",
                                       command=lambda: self._browse_cal_dir(self.darks_var))
        self._darks_browse.grid(row=0, column=3)
        self.darks_enabled_var.trace_add("write", self._on_darks_toggle)

        self._flats_row = tk.Frame(root, bg=C_BG)
        self._flats_row.grid(row=8, column=0, columnspan=3, sticky="ew", padx=20, pady=(8, 2))
        self._flats_row.columnconfigure(2, weight=1)
        self.flats_enabled_var = tk.BooleanVar(value=s.get("flats_enabled", True))
        self._flats_toggle = ToggleSwitch(self._flats_row, self.flats_enabled_var, on_color=C_ACCENT)
        self._flats_toggle.grid(row=0, column=0, padx=(0, 8))
        tk.Label(self._flats_row, text="Flats:", width=7, anchor="w",
                 bg=C_BG, fg=C_FG).grid(row=0, column=1)
        self.flats_var = tk.StringVar(value=self._flats_master_val if _use_master_init else self._flats_dir_val)
        self._flats_entry = tk.Entry(self._flats_row, textvariable=self.flats_var,
                                     bg=C_FIELD, fg=C_FG, insertbackground=C_FG, relief="flat")
        self._flats_entry.grid(row=0, column=2, sticky="ew", padx=(0, 4))
        self._flats_browse = tk.Button(self._flats_row, text="Browse",
                                       bg=C_BTN, fg=C_FG, activebackground=C_BTN_ACT, relief="flat",
                                       command=lambda: self._browse_cal_dir(self.flats_var))
        self._flats_browse.grid(row=0, column=3)
        self.flats_enabled_var.trace_add("write", self._on_flats_toggle)

        self._bias_row = tk.Frame(root, bg=C_BG)
        self._bias_row.grid(row=9, column=0, columnspan=3, sticky="ew", padx=20, pady=(8, 8))
        self._bias_row.columnconfigure(2, weight=1)
        self.bias_enabled_var = tk.BooleanVar(value=s.get("bias_enabled", False))
        self._bias_toggle = ToggleSwitch(self._bias_row, self.bias_enabled_var, on_color=C_ACCENT)
        self._bias_toggle.grid(row=0, column=0, padx=(0, 8))
        tk.Label(self._bias_row, text="Bias:", width=7, anchor="w",
                 bg=C_BG, fg=C_FG).grid(row=0, column=1)
        self.bias_var = tk.StringVar(value=self._bias_master_val if _use_master_init else self._bias_dir_val)
        self._bias_entry = tk.Entry(self._bias_row, textvariable=self.bias_var,
                                    bg=C_FIELD, fg=C_FG, insertbackground=C_FG, relief="flat")
        self._bias_entry.grid(row=0, column=2, sticky="ew", padx=(0, 4))
        self._bias_browse = tk.Button(self._bias_row, text="Browse",
                                      bg=C_BTN, fg=C_FG, activebackground=C_BTN_ACT, relief="flat",
                                      command=lambda: self._browse_cal_dir(self.bias_var))
        self._bias_browse.grid(row=0, column=3)
        self.bias_enabled_var.trace_add("write", self._on_bias_toggle)

        self._cal_sub_toggles  = [self._darks_toggle, self._flats_toggle, self._bias_toggle]
        self._darks_sub_widgets = [self._darks_entry, self._darks_browse]
        self._flats_sub_widgets = [self._flats_entry, self._flats_browse]
        self._bias_sub_widgets  = [self._bias_entry,  self._bias_browse]
        self._on_cal_toggle()

        # Master mode: frame type selector + sigma (row 10)
        self._master_type_row = tk.Frame(root, bg=C_BG)
        self._master_type_row.grid(row=10, column=0, columnspan=3,
                                   sticky="w", padx=20, pady=(8, 4))
        tk.Label(self._master_type_row, text="Frame Type:",
                 bg=C_BG, fg=C_FG, font=("", 11, "bold"), width=11, anchor="w").pack(side="left")

        saved_type = s.get("master_frame_type", "dark")
        self.master_dark_var = tk.BooleanVar(value=(saved_type == "dark"))
        self.master_flat_var = tk.BooleanVar(value=(saved_type == "flat"))
        self.master_bias_var = tk.BooleanVar(value=(saved_type == "bias"))
        # Guard: ensure exactly one is selected
        if not (self.master_dark_var.get() or
                self.master_flat_var.get() or
                self.master_bias_var.get()):
            self.master_dark_var.set(True)

        self._master_dark_ts = ToggleSwitch(self._master_type_row, self.master_dark_var,
                                            on_color=C_ACCENT)
        self._master_dark_ts.pack(side="left", padx=(0, 4))
        tk.Label(self._master_type_row, text="Darks",
                 bg=C_BG, fg=C_FG).pack(side="left", padx=(0, 18))

        self._master_flat_ts = ToggleSwitch(self._master_type_row, self.master_flat_var,
                                            on_color=C_ACCENT)
        self._master_flat_ts.pack(side="left", padx=(0, 4))
        tk.Label(self._master_type_row, text="Flats",
                 bg=C_BG, fg=C_FG).pack(side="left", padx=(0, 18))

        self._master_bias_ts = ToggleSwitch(self._master_type_row, self.master_bias_var,
                                            on_color=C_ACCENT)
        self._master_bias_ts.pack(side="left", padx=(0, 4))
        tk.Label(self._master_type_row, text="Bias",
                 bg=C_BG, fg=C_FG).pack(side="left", padx=(0, 28))

        tk.Label(self._master_type_row, text="Sigma clip:",
                 bg=C_BG, fg=C_FG).pack(side="left", padx=(0, 6))
        self.master_sigma_var = tk.StringVar(value=str(s.get("master_sigma", 3.0)))
        tk.Entry(self._master_type_row, textvariable=self.master_sigma_var, width=5,
                 bg=C_FIELD, fg=C_FG, insertbackground=C_FG, relief="flat").pack(side="left")
        tk.Label(self._master_type_row, text="σ (2.5–3.5)  •  Algorithm: median combine + σ-clip",
                 fg=C_HINT, bg=C_BG, font=("", 11)).pack(side="left", padx=(6, 0))

        # Wire radio-button traces after all vars are created
        self.master_dark_var.trace_add("write",
            lambda *_: self._on_master_type_toggle("dark"))
        self.master_flat_var.trace_add("write",
            lambda *_: self._on_master_type_toggle("flat"))
        self.master_bias_var.trace_add("write",
            lambda *_: self._on_master_type_toggle("bias"))

        # Master mode: build button row (row 11)
        self._master_btn_row = tk.Frame(root, bg=C_BG)
        self._master_btn_row.grid(row=11, column=0, columnspan=3, pady=(2, 8))
        self._master_build_btn = tk.Button(
            self._master_btn_row, text="▶  Build Master",
            bg=C_OK, fg=C_BG,
            activebackground="#88aa70", activeforeground=C_BG,
            font=("", 13, "bold"), command=self._build_master_start, width=11, anchor="center")
        self._master_build_btn.pack(side="left", padx=10)
        self._master_stop_btn = tk.Button(
            self._master_btn_row, text="■  Stop",
            bg=C_ERR, fg=C_FG,
            activebackground="#9e4a52", activeforeground=C_FG,
            font=("", 13, "bold"), command=self._build_master_cancel,
            width=8, state="disabled")
        self._master_stop_btn.pack(side="left", padx=10)

        # ── Platesolve ───────────────────────────────────────────────────────
        self._ps_section_lbl = self._section(root, "Platesolve", row=12)

        self._ps_toggle_row = tk.Frame(root, bg=C_BG)
        self._ps_toggle_row.grid(row=13, column=0, columnspan=3, sticky="w", padx=12, pady=(6, 2))
        self.ps_enabled_var = tk.BooleanVar(value=s.get("ps_enabled", True))
        ToggleSwitch(self._ps_toggle_row, self.ps_enabled_var, on_color=C_OK).pack(side="left")
        tk.Label(self._ps_toggle_row, text="Enable Platesolve",
                 font=("", 11), bg=C_BG, fg=C_FG).pack(side="left", padx=10)
        self.ps_enabled_var.trace_add("write", self._on_ps_toggle)

        self._astap_label = tk.Label(root, text="ASTAP Executable Path:", bg=C_BG, fg=C_FG)
        self._astap_label.grid(row=14, column=0, sticky="nw", padx=10, pady=(8, 4))
        self.astap_var = tk.StringVar(value=s.get("astap_exe", r"C:\Program Files\astap\astap.exe"))
        self._astap_frame = tk.Frame(root, bg=C_BG)
        self._astap_frame.grid(row=14, column=1, columnspan=2, padx=5, sticky="ew")
        self._astap_frame.columnconfigure(0, weight=1)
        self._astap_entry = tk.Entry(self._astap_frame, textvariable=self.astap_var,
                                     bg=C_FIELD, fg=C_FG, insertbackground=C_FG, relief="flat")
        self._astap_entry.grid(row=0, column=0, sticky="ew")
        self._astap_browse_btn = tk.Button(self._astap_frame, text="Browse",
                                           bg=C_BTN, fg=C_FG, activebackground=C_BTN_ACT, relief="flat",
                                           command=self._browse_astap)
        self._astap_browse_btn.grid(row=0, column=1, padx=4)
        tk.Label(self._astap_frame, text="Compatible with ASTAP only",
                 fg=C_HINT, font=("", 11), bg=C_BG).grid(row=1, column=0, sticky="w")

        self._radius_label = tk.Label(root, text="Search Radius (°):", bg=C_BG, fg=C_FG)
        self._radius_label.grid(row=15, column=0, sticky="w", **pad)
        self._radius_frame = tk.Frame(root, bg=C_BG)
        self._radius_frame.grid(row=15, column=1, columnspan=2, padx=5, sticky="w")
        self.radius_var = tk.StringVar(value=str(s.get("search_radius", 0.5)))
        self._radius_entry = tk.Entry(self._radius_frame, textvariable=self.radius_var, width=8,
                                      bg=C_FIELD, fg=C_FG, insertbackground=C_FG, relief="flat")
        self._radius_entry.grid(row=0, column=0, sticky="w")
        tk.Label(self._radius_frame, text="  (180 = blind solve; reduce when FOV is known)",
                 fg=C_HINT, font=("", 11), bg=C_BG).grid(row=0, column=1, sticky="w")

        self._ps_widgets = [self._astap_entry, self._astap_browse_btn, self._radius_entry]
        self._on_ps_toggle()

        # ── Monitor ───────────────────────────────────────────────────────────
        self._monitor_section_lbl = self._section(root, "Monitor", row=16)

        self._monitor_desc = tk.Frame(root, bg=C_BG)
        self._monitor_desc.grid(row=17, column=0, columnspan=3, sticky="w", padx=14, pady=(4, 2))
        tk.Label(self._monitor_desc,
                 text="Watch the Input Directory for new FITS files and process them automatically as they arrive.",
                 fg=C_HINT, font=("", 11), bg=C_BG).pack(side="left")

        self._btn_frame = tk.Frame(root, bg=C_BG)
        self._btn_frame.grid(row=18, column=0, columnspan=3, pady=8)
        self.start_btn = tk.Button(
            self._btn_frame, text="▶  Start Monitoring",
            bg=C_OK, fg=C_BG,
            activebackground="#88aa70", activeforeground=C_BG,
            font=("", 13, "bold"), command=self._start, padx=24)
        self.start_btn.pack(side="left", padx=10)
        self.stop_btn = tk.Button(
            self._btn_frame, text="■  Stop",
            bg=C_ERR, fg=C_FG,
            activebackground="#9e4a52", activeforeground=C_FG,
            font=("", 13, "bold"), command=self._stop, width=8, state="disabled")
        self.stop_btn.pack(side="left", padx=10)

        # Schedule start
        self._sched_start_frame = tk.Frame(root, bg=C_BG)
        self._sched_start_frame.grid(row=19, column=0, columnspan=3, sticky="w", padx=14, pady=(2, 2))
        self.schedule_enabled_var = tk.BooleanVar(value=s.get("schedule_enabled", False))
        self.schedule_time_var    = tk.StringVar(value=s.get("schedule_time", "20:00"))
        self._sched_cb = tk.Checkbutton(self._sched_start_frame, text="Schedule start:",
                                        variable=self.schedule_enabled_var,
                                        bg=C_BG, fg=C_FG,
                                        activebackground=C_BG, selectcolor=C_FIELD,
                                        command=self._on_schedule_toggle)
        self._sched_cb.pack(side="left")
        self._sched_time_entry = tk.Entry(self._sched_start_frame,
                                          textvariable=self.schedule_time_var, width=7,
                                          bg=C_FIELD, fg=C_FG, insertbackground=C_FG, relief="flat")
        self._sched_time_entry.pack(side="left", padx=4)
        tk.Label(self._sched_start_frame, text="HH:MM  (24-hour)",
                 fg=C_HINT, bg=C_BG).pack(side="left", padx=2)
        self._on_schedule_toggle()

        # Schedule stop
        self._sched_stop_frame = tk.Frame(root, bg=C_BG)
        self._sched_stop_frame.grid(row=20, column=0, columnspan=3, sticky="w", padx=14, pady=(2, 2))
        self.schedule_stop_enabled_var = tk.BooleanVar(value=s.get("schedule_stop_enabled", False))
        self.schedule_stop_time_var    = tk.StringVar(value=s.get("schedule_stop_time", "06:00"))
        self._sched_stop_cb = tk.Checkbutton(self._sched_stop_frame, text="Schedule stop: ",
                                             variable=self.schedule_stop_enabled_var,
                                             bg=C_BG, fg=C_FG,
                                             activebackground=C_BG, selectcolor=C_FIELD,
                                             command=self._on_stop_schedule_toggle)
        self._sched_stop_cb.pack(side="left")
        self._sched_stop_time_entry = tk.Entry(self._sched_stop_frame,
                                               textvariable=self.schedule_stop_time_var, width=7,
                                               bg=C_FIELD, fg=C_FG, insertbackground=C_FG, relief="flat")
        self._sched_stop_time_entry.pack(side="left", padx=4)
        tk.Label(self._sched_stop_frame, text="HH:MM  (24-hour)",
                 fg=C_HINT, bg=C_BG).pack(side="left", padx=2)
        tk.Label(self._sched_stop_frame, text="(can be changed while monitoring)",
                 fg=C_HINT, font=("", 11), bg=C_BG).pack(side="left", padx=10)
        self.sched_stop_status_var = tk.StringVar(value="")
        tk.Label(self._sched_stop_frame, textvariable=self.sched_stop_status_var,
                 font=("", 11, "bold"), fg=C_ERR, bg=C_BG).pack(side="left", padx=8)
        self._on_stop_schedule_toggle()

        # ── Process Existing Files ────────────────────────────────────────────
        self._pef_section_lbl = self._section(root, "Process Existing Files", row=21)

        self._batch_desc = tk.Frame(root, bg=C_BG)
        self._batch_desc.grid(row=22, column=0, columnspan=3, sticky="w", padx=14, pady=(4, 2))
        tk.Label(self._batch_desc,
                 text="Calibrate and/or platesolve all FITS files currently in the Input Directory.",
                 fg=C_HINT, font=("", 11), bg=C_BG).pack(side="left")

        self._batch_btn_frame = tk.Frame(root, bg=C_BG)
        self._batch_btn_frame.grid(row=23, column=0, columnspan=3, pady=(2, 8))
        self.batch_start_btn = tk.Button(
            self._batch_btn_frame, text="▶  Start",
            bg=C_OK, fg=C_BG,
            activebackground="#88aa70", activeforeground=C_BG,
            font=("", 13, "bold"), command=self._batch_start, width=9, anchor="center")
        self.batch_start_btn.pack(side="left", padx=10)
        self.batch_cancel_btn = tk.Button(
            self._batch_btn_frame, text="■  Stop",
            bg=C_ERR, fg=C_FG,
            activebackground="#9e4a52", activeforeground=C_FG,
            font=("", 13, "bold"), command=self._batch_cancel, width=8, state="disabled")
        self.batch_cancel_btn.pack(side="left", padx=10)

        # ── Status ───────────────────────────────────────────────────────────
        self._section(root, "Status", row=24)

        status_frame = tk.Frame(root, bg=C_BG)
        status_frame.grid(row=25, column=0, columnspan=3, sticky="w", padx=14, pady=(4, 2))
        tk.Label(status_frame, text="Status:", font=("", 8), bg=C_BG, fg=C_FG).pack(side="left")
        self.status_var = tk.StringVar(value="● Idle")
        self.status_lbl = tk.Label(status_frame, textvariable=self.status_var,
                                   font=("", 8, "bold"), fg=C_HINT, bg=C_BG)
        self.status_lbl.pack(side="left", padx=6)

        cal_ctr = tk.Frame(root, bg=C_BG)
        cal_ctr.grid(row=26, column=0, columnspan=3, sticky="w", padx=14, pady=(0, 1))
        tk.Label(cal_ctr, text="Calibration —", fg=C_FG, bg=C_BG).pack(side="left")
        tk.Label(cal_ctr, text="Calibrated:", bg=C_BG, fg=C_FG).pack(side="left", padx=(8, 0))
        self.cal_done_var = tk.StringVar(value="0 of 0")
        tk.Label(cal_ctr, textvariable=self.cal_done_var,
                 font=("", 11, "bold"), fg=C_ACCENT, bg=C_BG).pack(side="left", padx=(4, 12))
        tk.Label(cal_ctr, text="Failed:", bg=C_BG, fg=C_FG).pack(side="left")
        self.cal_failed_var = tk.StringVar(value="0")
        tk.Label(cal_ctr, textvariable=self.cal_failed_var,
                 font=("", 11, "bold"), fg=C_ERR, bg=C_BG).pack(side="left", padx=4)

        self._ps_ctr = tk.Frame(root, bg=C_BG)
        self._ps_ctr.grid(row=27, column=0, columnspan=3, sticky="w", padx=14, pady=(0, 6))
        tk.Label(self._ps_ctr, text="Platesolve —", fg=C_FG, bg=C_BG).pack(side="left")
        tk.Label(self._ps_ctr, text="Platesolved:", bg=C_BG, fg=C_FG).pack(side="left", padx=(8, 0))
        self.solved_var = tk.StringVar(value="0 of 0")
        tk.Label(self._ps_ctr, textvariable=self.solved_var,
                 font=("", 11, "bold"), fg=C_OK, bg=C_BG).pack(side="left", padx=(4, 12))
        tk.Label(self._ps_ctr, text="Failed:", bg=C_BG, fg=C_FG).pack(side="left")
        self.ps_failed_var = tk.StringVar(value="0")
        tk.Label(self._ps_ctr, textvariable=self.ps_failed_var,
                 font=("", 11, "bold"), fg=C_ERR, bg=C_BG).pack(side="left", padx=4)

        # ── Errors & Warnings ────────────────────────────────────────────────
        err_header = tk.Frame(root, bg="#3b2222")
        err_header.grid(row=28, column=0, columnspan=3, sticky="ew", padx=6, pady=(10, 0))
        tk.Label(err_header, text="  Errors & Warnings",
                 font=("", 11, "bold"), bg="#3b2222", fg=C_FG,
                 anchor="w").pack(side="left", fill="x", expand=True)
        tk.Button(err_header, text="Clear", font=("", 10),
                  bg=C_ERR, fg=C_FG, relief="flat", padx=8,
                  command=self._clear_errors).pack(side="right", padx=4, pady=2)

        err_frame = tk.Frame(root, bg=C_BG)
        err_frame.grid(row=29, column=0, columnspan=3, padx=10, pady=(0, 4), sticky="ew")
        self.error_widget = tk.Text(err_frame, height=6, width=80,
                                    state="disabled", font=("Courier", 11),
                                    bg=C_BG, fg=C_ERR, insertbackground=C_FG)
        self.error_widget.tag_config("error",   foreground=C_ERR)
        self.error_widget.tag_config("warning", foreground=C_WARN)
        self.error_widget.pack(side="left", fill="both", expand=True)
        err_sb = ttk.Scrollbar(err_frame, command=self.error_widget.yview)
        err_sb.pack(side="right", fill="y")
        self.error_widget["yscrollcommand"] = err_sb.set

        # ── Activity Log ─────────────────────────────────────────────────────
        self._section(root, "Activity Log", row=30)
        log_frame = tk.Frame(root, bg=C_BG)
        log_frame.grid(row=31, column=0, columnspan=3, padx=10, pady=4, sticky="nsew")
        root.columnconfigure(1, weight=1)
        self.log_widget = tk.Text(log_frame, height=14, width=80,
                                  state="disabled", font=("Courier", 11),
                                  bg=C_BG, fg=C_FG, insertbackground=C_FG)
        self.log_widget.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(log_frame, command=self.log_widget.yview)
        sb.pack(side="right", fill="y")
        self.log_widget["yscrollcommand"] = sb.set

        prune_log(LOG_FILE)
        self.logger = Logger(self.log_widget, LOG_FILE)
        self.logger.set_error_widget(self.error_widget)
        self.logger.info(f"Application started.  Log: {LOG_FILE}")
        if not _CCDPROC_OK:
            self.logger.warn("ccdproc / astropy not installed — Calibration unavailable.\n"
                             "  Run: pip install ccdproc astropy numpy")
        if s:
            self.logger.info("Previous settings restored.")

        # ── Copyright ────────────────────────────────────────────────────────
        tk.Label(root, text="© Art Trail 2026", font=("", 11), fg=C_HINT, bg=C_BG).grid(
            row=32, column=2, sticky="se", padx=8, pady=(0, 4))

        root.columnconfigure(1, weight=1)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Apply initial mode (show/hide correct sections, style buttons)
        self._on_mode_change(self._app_mode)

    # ── UI helpers ────────────────────────────────────────────────────────────

    def _section(self, parent, title, row) -> tk.Label:
        lbl = tk.Label(parent, text=f"  {title}",
                       font=("", 11, "bold"), bg=C_SEP, fg=C_ACCENT, anchor="w")
        lbl.grid(row=row, column=0, columnspan=3, sticky="ew", padx=6, pady=(10, 2))
        return lbl

    def _clear_errors(self):
        self.error_widget.config(state="normal")
        self.error_widget.delete("1.0", "end")
        self.error_widget.config(state="disabled")

    # ── Mode switching ────────────────────────────────────────────────────────

    def _on_mode_change(self, mode: str):
        self._app_mode = mode

        # Style the toggle buttons
        if mode == "science":
            self._mode_btn_science.config(bg=C_ACCENT, fg=C_BG, activebackground=C_ACCENT)
            self._mode_btn_master.config(bg=C_SEP,    fg=C_HINT, activebackground=C_SEP)
        else:
            self._mode_btn_science.config(bg=C_SEP,    fg=C_HINT, activebackground=C_SEP)
            self._mode_btn_master.config(bg=C_ACCENT,  fg=C_BG,   activebackground=C_ACCENT)

        # Widgets shown only in science mode
        _science_only = [
            self._cal_toggle_row,
            self._darks_row,
            self._flats_row,
            self._bias_row,
            self._ps_section_lbl,
            self._ps_toggle_row,
            self._astap_label,
            self._astap_frame,
            self._radius_label,
            self._radius_frame,
            self._monitor_section_lbl,
            self._monitor_desc,
            self._btn_frame,
            self._sched_start_frame,
            self._sched_stop_frame,
            self._pef_section_lbl,
            self._batch_desc,
            self._batch_btn_frame,
            self._ps_ctr,
        ]

        # Widgets shown only in master mode
        _master_only = [
            self._master_type_row,
            self._master_btn_row,
        ]

        if mode == "science":
            for w in _science_only:
                w.grid()
            for w in _master_only:
                w.grid_remove()
            self._input_dir_label.config(text="Input Directory:")
            self._cal_section_label.config(text="  Calibration")
        else:
            for w in _science_only:
                w.grid_remove()
            for w in _master_only:
                w.grid()
            self._input_dir_label.config(text="Source Frames Directory:")
            self._cal_section_label.config(text="  Create Master Cal Files")

    # ── Master frame type radio logic ─────────────────────────────────────────

    def _on_master_type_toggle(self, which: str):
        if self._master_type_updating:
            return
        self._master_type_updating = True
        try:
            var_map = {"dark": self.master_dark_var,
                       "flat": self.master_flat_var,
                       "bias": self.master_bias_var}
            if var_map[which].get():
                # Turn all others off
                for key, var in var_map.items():
                    if key != which:
                        var.set(False)
            else:
                # Prevent all-off: revert
                if not any(v.get() for v in var_map.values()):
                    var_map[which].set(True)
        finally:
            self._master_type_updating = False

    # ── Master build ──────────────────────────────────────────────────────────

    def _build_master_start(self):
        input_dir_str  = self.watch_var.get().strip()
        output_dir_str = self.output_var.get().strip()

        errors = []
        if not input_dir_str or not Path(input_dir_str).is_dir():
            errors.append("• Source Frames Directory does not exist or is not set.")
        if not output_dir_str:
            errors.append("• Output Directory is not set.")
        if not _CCDPROC_OK:
            errors.append("• ccdproc/astropy not installed.  "
                          "Run: pip install ccdproc astropy numpy")
        if errors:
            messagebox.showerror("Cannot Build",
                "Please fix the following:\n\n" + "\n".join(errors))
            return

        save_settings(self._collect_settings())

        self._master_cancel_event = threading.Event()
        self._master_build_btn.config(state="disabled")
        self._master_stop_btn.config(state="normal")
        self._mode_btn_science.config(state="disabled")
        self._mode_btn_master.config(state="disabled")
        for ts in (self._master_dark_ts, self._master_flat_ts, self._master_bias_ts):
            ts.set_enabled(False)
        self._update_status("⚙  Building master …", C_ACCENT)

        self.logger.info("=" * 60)
        self.logger.info("Master build STARTED")
        self.logger.info(f"  Source dir  : {input_dir_str}")
        self.logger.info(f"  Output dir  : {output_dir_str}")

        threading.Thread(target=self._build_master_thread, daemon=True).start()

    def _build_master_thread(self):
        if self.master_dark_var.get():
            frame_type = "dark"
        elif self.master_flat_var.get():
            frame_type = "flat"
        else:
            frame_type = "bias"

        sigma = self._parse_master_sigma()

        success = build_and_save_master(
            input_dir           = Path(self.watch_var.get().strip()),
            output_dir          = Path(self.output_var.get().strip()),
            frame_type          = frame_type,
            sigma               = sigma,
            logger              = self.logger,
            cal_counter_updater = lambda d, f, t: self.root.after(
                                      0, self._update_cal_counters, d, f, t),
            cancel_event        = self._master_cancel_event,
        )
        self.root.after(0, self._build_master_done, success)

    def _build_master_done(self, success: bool):
        self._master_build_btn.config(state="normal")
        self._master_stop_btn.config(state="disabled")
        self._mode_btn_science.config(state="normal")
        self._mode_btn_master.config(state="normal")
        for ts in (self._master_dark_ts, self._master_flat_ts, self._master_bias_ts):
            ts.set_enabled(True)
        self._master_cancel_event = None
        self._update_status("● Idle", C_HINT)
        if success:
            self.logger.info("Master build DONE")
        else:
            self.logger.warn("Master build FAILED or was cancelled")
        self.logger.info("=" * 60)
        save_settings(self._collect_settings())

    def _build_master_cancel(self):
        if self._master_cancel_event:
            self._master_cancel_event.set()
        self._master_stop_btn.config(state="disabled")

    # ── Instructions ──────────────────────────────────────────────────────────

    def _show_instructions(self):
        win = tk.Toplevel(self.root)
        win.title("FITS Calibrator — User Guide")
        win.geometry("820x700")
        win.resizable(True, True)
        win.configure(bg=C_BG)

        title = tk.Label(win, text="FITS Calibrator — User Guide",
                         font=("", 13, "bold"), bg=C_BG2, fg=C_FG,
                         anchor="w", padx=14, pady=8)
        title.pack(fill="x")

        txt_frame = tk.Frame(win, bg=C_BG)
        txt_frame.pack(fill="both", expand=True, padx=14, pady=10)

        txt = tk.Text(txt_frame, wrap="word", font=("", 10),
                      bg=C_BG2, fg=C_FG, relief="flat",
                      padx=6, pady=6, state="normal",
                      insertbackground=C_FG)
        sb = ttk.Scrollbar(txt_frame, command=txt.yview)
        txt["yscrollcommand"] = sb.set
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)

        txt.tag_config("h", font=("", 10, "bold"), foreground=C_ACCENT,
                       spacing1=10, spacing3=2)
        txt.tag_config("body", font=("", 10), foreground=C_FG,
                       lmargin1=14, lmargin2=14, spacing3=3)
        txt.tag_config("note", font=("", 9, "italic"), foreground=C_HINT,
                       lmargin1=14, lmargin2=14, spacing3=6)

        sections = [
            ("Mode Toggle",
             "Calibrate Science Frames — the full pipeline: calibrate and/or platesolve your "
             "incoming light frames in real time or as a batch.\n"
             "Create Master Cal Files — build a single master dark, flat, or bias from a folder "
             "of raw calibration frames.  Use this mode when you want to produce standalone master "
             "frames for use in other software.  It is not required for calibrating science frames "
             "in this app — see Calibration below.",
             None),
            ("Directories",
             "Input Directory — the folder containing the incoming FITS files (or source "
             "calibration frames in Create Master mode).\n"
             "Output Directory — where processed files are written (defaults to Input Directory).\n"
             "A 'Calibrated' subfolder is created automatically inside the Output Directory "
             "when calibrating science frames.",
             None),
            ("Calibration (Science mode)",
             "Enable Calibration, then choose a calibration source mode:\n"
             "Raw Frames mode (Use master files OFF) — point the Darks, Flats, and/or Bias "
             "toggles at folders containing your raw calibration frames.  FITS Calibrator "
             "builds the master frames automatically at start.\n"
             "Master Files mode (Use master files ON) — point each toggle directly at a "
             "pre-made master FITS file.  No combining is performed; the file is loaded as-is.\n"
             "Output file names get a '_CAL' suffix (e.g. image_CAL.fits).\n"
             "Algorithm: bias subtract → dark subtract (exposure-scaled) → flat correct, "
             "applied in that order via ccdproc.  Any step whose toggle is off is skipped.",
             None),
            ("Create Master Cal Files",
             "Select the frame type (Darks, Flats, or Bias) and point the Source Frames "
             "Directory at the folder containing the raw calibration frames.\n"
             "Algorithm: median combine all frames with sigma-clipped outlier rejection (via ccdproc).\n"
             "Sigma clip — controls outlier rejection during combine (default 3.0).  "
             "Lower values reject more aggressively; typical range is 2.5 – 3.5.\n"
             "The master is saved directly to the Output Directory as "
             "master_dark.fits, master_flat.fits, or master_bias.fits.\n"
             "This mode is optional — FITS Calibrator can build masters on the fly when "
             "calibrating science frames.  Use this mode only when you need a saved master "
             "file for use outside this application.",
             None),
            ("Platesolve (Science mode)",
             "Enable Platesolve and set the path to the ASTAP executable.  "
             "ASTAP embeds WCS coordinates directly into the FITS header.\n"
             "Solved files get a '_WCS' suffix (e.g. image_CAL_WCS.fits).  "
             "A smaller search radius solves faster when your FOV is known.",
             "Install ASTAP from: https://www.hnsky.org/astap.htm"),
            ("Monitor (Science mode)",
             "Watches the directory in real time — every new FITS file that arrives is "
             "automatically calibrated and/or platesolved as it lands.\n"
             "Use Schedule Start / Stop to set automatic on/off times (HH:MM, 24-hour).",
             None),
            ("Process Existing Files (Science mode)",
             "One-shot run: calibrates and/or platesolves every FITS file already present "
             "in the Input Directory, then finishes.  No folder watching.",
             None),
            ("Tips",
             "• Output and Input directories can be the same folder.\n"
             "• You do not need to pre-create master frames — FITS Calibrator builds them "
             "automatically from your raw calibration frame folders.\n"
             "• Settings are saved automatically on Start and on close.\n"
             "• Errors and warnings persist in the Errors & Warnings panel until cleared.",
             None),
        ]

        for heading, body, note in sections:
            txt.insert("end", f"{heading}\n", "h")
            txt.insert("end", f"{body}\n", "body")
            if note:
                txt.insert("end", f"{note}\n", "note")

        txt.config(state="disabled")

    # ── Browse helpers ────────────────────────────────────────────────────────

    def _browse_watch(self):
        current = self.watch_var.get()
        init = current if current and Path(current).is_dir() else ""
        d = filedialog.askdirectory(initialdir=init)
        if d:
            old_watch = self.watch_var.get()
            self.watch_var.set(d)
            if not self.output_var.get() or self.output_var.get() == old_watch:
                self.output_var.set(d)

    def _browse_output(self):
        current = self.output_var.get()
        init = current if current and Path(current).is_dir() else ""
        d = filedialog.askdirectory(initialdir=init)
        if d:
            self.output_var.set(d)

    def _browse_astap(self):
        f = filedialog.askopenfilename(
            title="Select solver executable",
            filetypes=[("Executable", "*.exe"), ("All files", "*.*")])
        if f:
            self.astap_var.set(f)

    def _browse_cal_dir(self, var: tk.StringVar):
        if self.cal_use_master_var.get():
            f = filedialog.askopenfilename(
                title="Select master FITS file",
                filetypes=[("FITS files", "*.fits *.fit *.fts"), ("All files", "*.*")])
            if f:
                var.set(f)
        else:
            d = filedialog.askdirectory()
            if d:
                var.set(d)

    # ── Toggle callbacks ──────────────────────────────────────────────────────

    def _on_schedule_toggle(self, *_):
        self._sched_time_entry.config(
            state="normal" if self.schedule_enabled_var.get() else "disabled")

    def _on_stop_schedule_toggle(self, *_):
        self._sched_stop_time_entry.config(
            state="normal" if self.schedule_stop_enabled_var.get() else "disabled")

    def _on_ps_toggle(self, *_):
        state = "normal" if self.ps_enabled_var.get() else "disabled"
        for w in self._ps_widgets:
            try:
                w.config(state=state)
            except tk.TclError:
                pass

    def _on_cal_toggle(self, *_):
        enabled = self.cal_enabled_var.get()
        for t in self._cal_sub_toggles:
            t.set_enabled(enabled)
        self._on_darks_toggle()
        self._on_flats_toggle()
        self._on_bias_toggle()
        self.cal_use_master_var.set(self.cal_use_master_var.get())

    def _on_darks_toggle(self, *_):
        state = "normal" if (self.cal_enabled_var.get() and
                             self.darks_enabled_var.get()) else "disabled"
        for w in self._darks_sub_widgets:
            try:
                w.config(state=state)
            except tk.TclError:
                pass

    def _on_flats_toggle(self, *_):
        state = "normal" if (self.cal_enabled_var.get() and
                             self.flats_enabled_var.get()) else "disabled"
        for w in self._flats_sub_widgets:
            try:
                w.config(state=state)
            except tk.TclError:
                pass

    def _on_bias_toggle(self, *_):
        state = "normal" if (self.cal_enabled_var.get() and
                             self.bias_enabled_var.get()) else "disabled"
        for w in self._bias_sub_widgets:
            try:
                w.config(state=state)
            except tk.TclError:
                pass

    def _on_cal_source_toggle(self, *_):
        use_master = self.cal_use_master_var.get()
        self._cal_raw_lbl.config(fg=C_HINT if use_master else C_ACCENT)
        self._cal_master_lbl.config(fg=C_WARN if use_master else C_HINT)
        for (var, dir_attr, master_attr) in [
            (self.darks_var, "_darks_dir_val", "_darks_master_val"),
            (self.flats_var, "_flats_dir_val", "_flats_master_val"),
            (self.bias_var,  "_bias_dir_val",  "_bias_master_val"),
        ]:
            if use_master:
                setattr(self, dir_attr, var.get())
                var.set(getattr(self, master_attr))
            else:
                setattr(self, master_attr, var.get())
                var.set(getattr(self, dir_attr))

    def _update_status(self, text: str, color: str):
        self.status_var.set(text)
        self.status_lbl.config(fg=color)

    def _update_sched_stop_status(self, text: str):
        self.sched_stop_status_var.set(text)

    def _update_ps_counters(self, solved: int, failed: int, total: int):
        self.solved_var.set(f"{solved} of {total}")
        self.ps_failed_var.set(str(failed))

    def _update_cal_counters(self, done: int, failed: int, total: int):
        self.cal_done_var.set(f"{done} of {total}")
        self.cal_failed_var.set(str(failed))

    # ── Settings ──────────────────────────────────────────────────────────────

    def _collect_settings(self) -> dict:
        return {
            "watch_dir":             self.watch_var.get(),
            "output_dir":            self.output_var.get(),
            "ps_enabled":            self.ps_enabled_var.get(),
            "astap_exe":             self.astap_var.get(),
            "search_radius":         self._parse_radius(),
            "cal_enabled":           self.cal_enabled_var.get(),
            "cal_use_master":        self.cal_use_master_var.get(),
            "darks_enabled":         self.darks_enabled_var.get(),
            "darks_dir":             (self._darks_dir_val if self.cal_use_master_var.get()
                                      else self.darks_var.get()),
            "darks_master_file":     (self.darks_var.get() if self.cal_use_master_var.get()
                                      else self._darks_master_val),
            "flats_enabled":         self.flats_enabled_var.get(),
            "flats_dir":             (self._flats_dir_val if self.cal_use_master_var.get()
                                      else self.flats_var.get()),
            "flats_master_file":     (self.flats_var.get() if self.cal_use_master_var.get()
                                      else self._flats_master_val),
            "bias_enabled":          self.bias_enabled_var.get(),
            "bias_dir":              (self._bias_dir_val  if self.cal_use_master_var.get()
                                      else self.bias_var.get()),
            "bias_master_file":      (self.bias_var.get() if self.cal_use_master_var.get()
                                      else self._bias_master_val),
            "schedule_enabled":      self.schedule_enabled_var.get(),
            "schedule_time":         self.schedule_time_var.get(),
            "schedule_stop_enabled": self.schedule_stop_enabled_var.get(),
            "schedule_stop_time":    self.schedule_stop_time_var.get(),
            "app_mode":              self._app_mode,
            "master_frame_type":     ("dark"  if self.master_dark_var.get() else
                                      "flat"  if self.master_flat_var.get() else "bias"),
            "master_sigma":          self._parse_master_sigma(),
        }

    def _parse_radius(self) -> float:
        try:
            r = float(self.radius_var.get())
            if r <= 0:
                raise ValueError
            return r
        except ValueError:
            return 0.5

    def _parse_master_sigma(self) -> float:
        try:
            v = float(self.master_sigma_var.get())
            return v if v > 0 else 3.0
        except ValueError:
            return 3.0

    # ── Start / Stop (science mode) ───────────────────────────────────────────

    def _start(self):
        watch_dir = Path(self.watch_var.get().strip())
        astap_exe = self.astap_var.get().strip()

        errors = []
        if not watch_dir or not watch_dir.is_dir():
            errors.append("• Input Directory does not exist or is not set.")
        if not self.output_var.get().strip():
            errors.append("• Output Directory is not set.")
        if self.ps_enabled_var.get() and not astap_exe:
            errors.append("• ASTAP Executable Path is not set.")
        if self.cal_enabled_var.get():
            if not _CCDPROC_OK:
                errors.append("• ccdproc/astropy not installed.  "
                               "Run: pip install ccdproc astropy numpy")
            any_cal = (
                (self.darks_enabled_var.get() and self.darks_var.get().strip()) or
                (self.flats_enabled_var.get() and self.flats_var.get().strip()) or
                (self.bias_enabled_var.get()  and self.bias_var.get().strip())
            )
            if not any_cal:
                kind = "files" if self.cal_use_master_var.get() else "folders"
                errors.append(f"• Calibration is enabled but no Dark/Flat/Bias {kind} are set.")
        if self.schedule_stop_enabled_var.get():
            try:
                self._parse_schedule_time(self.schedule_stop_time_var.get().strip())
            except ValueError:
                errors.append(f"• Schedule Stop time '{self.schedule_stop_time_var.get()}' "
                               f"is not valid.  Please use HH:MM in 24-hour format.")
        if errors:
            messagebox.showerror("Cannot Start",
                "Please fix the following before starting:\n\n" + "\n".join(errors))
            return

        save_settings(self._collect_settings())

        if self.schedule_enabled_var.get():
            time_str = self.schedule_time_var.get().strip()
            try:
                target_time = self._parse_schedule_time(time_str)
            except ValueError:
                messagebox.showerror("Invalid Schedule Time",
                    f"'{time_str}' is not a valid time.\nPlease use HH:MM in 24-hour format.")
                return
            self._arm_scheduled_start(target_time)
        else:
            self._begin_monitoring()

    def _begin_monitoring(self):
        watch_dir  = Path(self.watch_var.get().strip())
        output_dir = Path(self.output_var.get().strip())
        astap_exe  = self.astap_var.get().strip()
        radius     = self._parse_radius()

        output_dir.mkdir(parents=True, exist_ok=True)

        cal_enabled = self.cal_enabled_var.get() and _CCDPROC_OK
        darks_dir = (Path(self.darks_var.get().strip())
                     if cal_enabled and self.darks_enabled_var.get()
                     and self.darks_var.get().strip() else None)
        flats_dir = (Path(self.flats_var.get().strip())
                     if cal_enabled and self.flats_enabled_var.get()
                     and self.flats_var.get().strip() else None)
        bias_dir  = (Path(self.bias_var.get().strip())
                     if cal_enabled and self.bias_enabled_var.get()
                     and self.bias_var.get().strip() else None)

        self.watcher = FITSWatcher(
            watch_dir           = watch_dir,
            output_dir          = output_dir,
            solver_exe          = astap_exe,
            search_radius       = radius,
            logger              = self.logger,
            platesolve_enabled  = self.ps_enabled_var.get(),
            cal_enabled         = cal_enabled,
            darks_dir           = darks_dir,
            flats_dir           = flats_dir,
            bias_dir            = bias_dir,
            cal_source          = "master" if self.cal_use_master_var.get() else "raw",
            status_updater      = lambda t, c: self.root.after(0, self._update_status, t, c),
            ps_counter_updater  = lambda s, f, t: self.root.after(0, self._update_ps_counters, s, f, t),
            cal_counter_updater = lambda d, f, t: self.root.after(0, self._update_cal_counters, d, f, t),
        )

        self.observer = Observer()
        self.observer.schedule(self.watcher, str(watch_dir), recursive=True)
        self.observer.start()

        threading.Thread(target=self._init_pipeline, daemon=True).start()

        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.batch_start_btn.config(state="disabled")
        self._mode_btn_science.config(state="disabled")
        self._mode_btn_master.config(state="disabled")
        self._update_status("● Watching", C_ACCENT)

        self.logger.info("=" * 60)
        self.logger.info("Monitoring STARTED")
        self.logger.info(f"  Input dir   : {watch_dir}")
        self.logger.info(f"  Output dir  : {output_dir}")
        self.logger.info(f"  Platesolve  : {'ON' if self.ps_enabled_var.get() else 'OFF'}")
        if self.ps_enabled_var.get():
            self.logger.info(f"  Solver      : {astap_exe}")
            self.logger.info(f"  Radius      : {radius}°")
        self.logger.info(f"  Calibration : {'ON' if cal_enabled else 'OFF'}")
        if cal_enabled:
            self.logger.info(f"  Darks       : {darks_dir or '(disabled)'}")
            self.logger.info(f"  Flats       : {flats_dir or '(disabled)'}")
            self.logger.info(f"  Bias        : {bias_dir  or '(disabled)'}")

        if self.schedule_stop_enabled_var.get():
            self._arm_scheduled_stop()

    def _init_pipeline(self):
        if self.watcher:
            self.watcher.build_masters()
            self.watcher.scan_existing_files()

    def _stop(self):
        for attr in ("_schedule_cancel", "_stop_schedule_cancel"):
            ev = getattr(self, attr, None)
            if ev is not None:
                ev.set()
        if self.watcher:
            self.watcher.cancel()
        if self.observer:
            self.observer.stop()
            self.observer.join()
            self.observer = None
        self.watcher = None
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        if self._batch_watcher is None:
            self.batch_start_btn.config(state="normal")
        self._mode_btn_science.config(state="normal")
        self._mode_btn_master.config(state="normal")
        self.sched_stop_status_var.set("")
        self._update_status("● Idle", C_HINT)
        self.logger.info("Monitoring STOPPED")
        self.logger.info("=" * 60)
        save_settings(self._collect_settings())

    # ── Schedule helpers ──────────────────────────────────────────────────────

    def _parse_schedule_time(self, time_str: str) -> datetime:
        t = datetime.strptime(time_str, "%H:%M")
        now = datetime.now()
        target = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    def _arm_scheduled_start(self, target_time: datetime):
        self._schedule_cancel = threading.Event()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.logger.info(f"Monitoring scheduled to start at "
                         f"{target_time.strftime('%H:%M')} on "
                         f"{target_time.strftime('%Y-%m-%d')}")
        threading.Thread(target=self._schedule_worker,
                         args=(target_time, self._schedule_cancel),
                         daemon=True).start()

    def _schedule_worker(self, target_time: datetime, cancel_event: threading.Event):
        while not cancel_event.is_set():
            remaining = (target_time - datetime.now()).total_seconds()
            if remaining <= 0:
                break
            hours, rem = divmod(int(remaining), 3600)
            mins, secs = divmod(rem, 60)
            countdown = f"{hours}h {mins:02d}m" if hours > 0 else f"{mins}m {secs:02d}s"
            self.root.after(0, self._update_status,
                            f"\u23f0  Starts at {target_time.strftime('%H:%M')} (in {countdown})",
                            C_ACCENT)
            time.sleep(1)
        if not cancel_event.is_set():
            self.root.after(0, self._begin_monitoring)

    def _arm_scheduled_stop(self):
        self._stop_schedule_cancel = threading.Event()
        threading.Thread(target=self._schedule_stop_worker,
                         args=(self._stop_schedule_cancel,),
                         daemon=True).start()

    def _schedule_stop_worker(self, cancel_event: threading.Event):
        last_target = None
        while not cancel_event.is_set():
            time_str = self.schedule_stop_time_var.get().strip()
            try:
                target_time = self._parse_schedule_time(time_str)
                if target_time != last_target:
                    self.logger.info(
                        f"Scheduled stop {'updated' if last_target else 'set'} to "
                        f"{target_time.strftime('%H:%M')} on {target_time.strftime('%Y-%m-%d')}")
                    last_target = target_time
            except ValueError:
                target_time = last_target
            if target_time is None:
                time.sleep(1)
                continue
            remaining = (target_time - datetime.now()).total_seconds()
            if remaining <= 0:
                break
            hours, rem = divmod(int(remaining), 3600)
            mins, secs = divmod(rem, 60)
            countdown = f"{hours}h {mins:02d}m" if hours > 0 else f"{mins}m {secs:02d}s"
            self.root.after(0, self._update_sched_stop_status,
                            f"\u23f0  Stops at {target_time.strftime('%H:%M')} (in {countdown})")
            time.sleep(1)
        if not cancel_event.is_set():
            self.root.after(0, self._stop)

    # ── Batch processing ──────────────────────────────────────────────────────

    def _batch_start(self):
        watch_dir = Path(self.watch_var.get().strip())
        astap_exe = self.astap_var.get().strip()

        errors = []
        if not watch_dir or not watch_dir.is_dir():
            errors.append("• Input Directory does not exist or is not set.")
        if not self.output_var.get().strip():
            errors.append("• Output Directory is not set.")
        if self.ps_enabled_var.get() and not astap_exe:
            errors.append("• ASTAP Executable Path is not set.")
        if self.cal_enabled_var.get():
            if not _CCDPROC_OK:
                errors.append("• ccdproc/astropy not installed.  "
                               "Run: pip install ccdproc astropy numpy")
            any_cal = (
                (self.darks_enabled_var.get() and self.darks_var.get().strip()) or
                (self.flats_enabled_var.get() and self.flats_var.get().strip()) or
                (self.bias_enabled_var.get()  and self.bias_var.get().strip())
            )
            if not any_cal:
                kind = "files" if self.cal_use_master_var.get() else "folders"
                errors.append(f"• Calibration is enabled but no Dark/Flat/Bias {kind} are set.")
        if not self.cal_enabled_var.get() and not self.ps_enabled_var.get():
            errors.append("• Both Calibration and Platesolve are disabled — nothing to do.")
        if errors:
            messagebox.showerror("Cannot Start",
                "Please fix the following before starting:\n\n" + "\n".join(errors))
            return

        save_settings(self._collect_settings())

        output_dir = Path(self.output_var.get().strip())
        output_dir.mkdir(parents=True, exist_ok=True)
        radius = self._parse_radius()

        cal_enabled = self.cal_enabled_var.get() and _CCDPROC_OK
        darks_dir = (Path(self.darks_var.get().strip())
                     if cal_enabled and self.darks_enabled_var.get()
                     and self.darks_var.get().strip() else None)
        flats_dir = (Path(self.flats_var.get().strip())
                     if cal_enabled and self.flats_enabled_var.get()
                     and self.flats_var.get().strip() else None)
        bias_dir  = (Path(self.bias_var.get().strip())
                     if cal_enabled and self.bias_enabled_var.get()
                     and self.bias_var.get().strip() else None)

        def _batch_status(text, color):
            if text == "● Watching":
                return
            self.root.after(0, self._update_status, text, color)

        self._batch_watcher = FITSWatcher(
            watch_dir           = watch_dir,
            output_dir          = output_dir,
            solver_exe          = astap_exe,
            search_radius       = radius,
            logger              = self.logger,
            platesolve_enabled  = self.ps_enabled_var.get(),
            cal_enabled         = cal_enabled,
            darks_dir           = darks_dir,
            flats_dir           = flats_dir,
            bias_dir            = bias_dir,
            cal_source          = "master" if self.cal_use_master_var.get() else "raw",
            status_updater      = _batch_status,
            ps_counter_updater  = lambda s, f, t: self.root.after(0, self._update_ps_counters, s, f, t),
            cal_counter_updater = lambda d, f, t: self.root.after(0, self._update_cal_counters, d, f, t),
        )

        self.batch_start_btn.config(state="disabled")
        self.batch_cancel_btn.config(state="normal")
        self.start_btn.config(state="disabled")
        self._mode_btn_science.config(state="disabled")
        self._mode_btn_master.config(state="disabled")
        self._update_status("⚙  Processing ...", C_ACCENT)

        self.logger.info("=" * 60)
        self.logger.info("Batch processing STARTED")
        self.logger.info(f"  Input dir   : {watch_dir}")
        self.logger.info(f"  Output dir  : {output_dir}")
        self.logger.info(f"  Platesolve  : {'ON' if self.ps_enabled_var.get() else 'OFF'}")
        if self.ps_enabled_var.get():
            self.logger.info(f"  Solver      : {astap_exe}")
            self.logger.info(f"  Radius      : {radius}°")
        self.logger.info(f"  Calibration : {'ON' if cal_enabled else 'OFF'}")
        if cal_enabled:
            self.logger.info(f"  Darks       : {darks_dir or '(disabled)'}")
            self.logger.info(f"  Flats       : {flats_dir or '(disabled)'}")
            self.logger.info(f"  Bias        : {bias_dir  or '(disabled)'}")

        threading.Thread(target=self._batch_pipeline, daemon=True).start()

    def _batch_pipeline(self):
        if self._batch_watcher:
            self._batch_watcher.build_masters()
            self._batch_watcher.scan_existing_files()
        self.root.after(0, self._batch_done)

    def _batch_cancel(self):
        if self._batch_watcher:
            self._batch_watcher.cancel()

    def _batch_done(self):
        self._batch_watcher = None
        self.batch_start_btn.config(state="normal")
        self.batch_cancel_btn.config(state="disabled")
        if self.observer is None:
            self.start_btn.config(state="normal")
        self._mode_btn_science.config(state="normal")
        self._mode_btn_master.config(state="normal")
        self._update_status("● Idle", C_HINT)
        self.logger.info("Batch processing DONE")
        self.logger.info("=" * 60)

    def _on_close(self):
        for attr in ("_schedule_cancel", "_stop_schedule_cancel"):
            ev = getattr(self, attr, None)
            if ev is not None:
                ev.set()
        if self._master_cancel_event:
            self._master_cancel_event.set()
        if self._batch_watcher:
            self._batch_watcher.cancel()
        if self.observer:
            self.observer.stop()
            self.observer.join()
        save_settings(self._collect_settings())
        self.root.destroy()

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    root = tk.Tk()
    app  = App(root)
    root.mainloop()
