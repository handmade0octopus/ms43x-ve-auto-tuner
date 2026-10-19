#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MS43X VE auto-tuner — local web app (DATASTAR frontend, FastAPI backend).

Run:  python server.py   ->  opens http://127.0.0.1:<port> in your browser.
Build: pyinstaller --onefile server.py -n MS43XVEAutoTuner --add-data "web;web"

The 8 VE tables are treated as one 3D histogram (VANOS table blend x MAP x RPM);
log samples are accumulated into it and per-cell corrections derived.
"""
import io
import asyncio
import collections
import contextvars
import csv
import json
import os
import re
import secrets
import string
import sys
import threading
import time
import webbrowser
from urllib.parse import quote

import numpy as np
from fastapi import FastAPI, Depends, Request
from fastapi.responses import FileResponse, Response, HTMLResponse
from fastapi.staticfiles import StaticFiles

from datastar_py import ServerSentEventGenerator as SSE
from datastar_py.fastapi import DatastarResponse, read_signals

from core.logs import _norm
from core import (parse_xdf_data, find_ve_family, resolve_axis, LogFile,
                  guess_channels, guess_map_scale, Tuner, read_bytes_robust,
                  write_bytes_robust)

APP_NAME = "MS43X VE auto-tuner"
APP_VERSION = "2.0.0"

# ----------------------------------------------------------------------------- 
# colors (heatmap)
# ----------------------------------------------------------------------------- 
HEAT_LEAN = (0xd2, 0x4b, 0x3e)     # needs more fuel (was lean)
HEAT_RICH = (0x38, 0x8d, 0xdb)     # needs less fuel (was rich)
HEAT_NONE = (0x21, 0x25, 0x2b)
HEAT_COUNT = (0x2e, 0xa0, 0x43)


def lerp(c1, c2, t):
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def heat_bg(delta, vmax):
    """Background tint for a cell: red = was lean (fuel added), blue = was rich.
    Any non-zero change gets a clearly visible minimum tint, scaling up to full
    colour at vmax."""
    if delta is None or not np.isfinite(delta) or abs(delta) < 1e-9:
        return HEAT_NONE
    t = min(1.0, abs(delta) / max(vmax, 1e-9))
    tgt = HEAT_LEAN if delta > 0 else HEAT_RICH
    return lerp(HEAT_NONE, tgt, 0.55 + 0.45 * t)


def css(c):
    return '#%02x%02x%02x' % c


def esc(s):
    import html
    return html.escape(str(s), quote=True)


VIEW_MODES = ['tuned', 'orig', 'delta', 'factor', 'samples']

PARAM_DEFAULTS = {
    'rpmMin': 300, 'rpmMax': 7500, 'afrMin': 10.0, 'afrMax': 19.0,
    'useClt': True, 'cltMin': 60.0,
    'useTrans': False, 'transRpm': 2.0, 'transMap': 200.0,
    'strength': 100, 'maxChange': 15.0, 'minSamples': 5.0,
    'smoothing': 0.0, 'blend': True, 'stoich': 14.7,
}

CHANNEL_KEYS = ['rpm', 'map', 'vetable', 'afrtarget', 'afr1', 'afr2',
                'stft1', 'stft2', 'ltft1', 'ltft2', 'inj', 'clt', 'time']

# Web deployment mode (--web / VE_WEB=1): no native dialogs or app window;
# files come in through browser uploads / drag & drop and results are
# downloaded through the browser instead of native Save As dialogs.
WEB_MODE = bool(os.environ.get('VE_WEB') or '--web' in sys.argv[1:])
# total upload size accepted by /api/upload (guard for public deployments)
MAX_UPLOAD = int(os.environ.get('VE_MAX_UPLOAD_MB', '256')) * 1024 * 1024


def resource_path(rel):
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def state_store_path():
    d = (os.environ.get('VE_DATA_DIR')
         or os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'),
                         'MS4X-VE-AutoTune'))
    try:
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, 'state.json')
    except OSError:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'state.json')


# -----------------------------------------------------------------------------
# application state — one instance per browser session (web) or a single
# instance for the local desktop user. Web sessions live in RAM only: nothing
# is ever written to disk.
# -----------------------------------------------------------------------------
class AppState:
    def __init__(self):
        self.messages = []
        # definition / binary
        self.xdf_path = None
        self.xdf_base = 0
        self.xdf_tables = {}
        self.ve_tables = []
        self.bin_path = None
        self.bin_data = None
        self.tuner = Tuner()
        # logs
        self.logs = []                     # list[LogFile]
        self.channels = {}                 # key -> column index or None
        self.map_scale = 100.0
        # analysis
        self.analysis_valid = False
        self.apply_valid = False
        # view / session options
        self.view_mode = 'tuned'
        self.show_table = 0            # 0 = show all, 1..N = table index+1
        self.corr_mode = 'afr'         # 'afr' | 'afr+trim' | 'trim'
        self.afr_src = 'avg'           # 'avg' | 'b1' | 'b2'
        self.units = 'afr'             # wideband/target logged as 'afr' or 'lambda'
        self.stoich = 14.7             # stoich AFR used to convert lambda -> AFR
        self.param_view = None         # None | 'afr' | 'target' | 'lambda' | ('col', idx)
        self._last_smoothing = 0
        self._last_min_samples = 5.0
        self._last_max_change = 15.0
        # persistence (desktop only — web sessions keep everything in memory)
        self.store = None if WEB_MODE else state_store_path()
        self.recent = {'xdf': [], 'bin': [], 'log': []}
        self.mem_files = {'xdf': [], 'bin': [], 'log': []}   # (name, bytes)
        self.dirs = {'xdf': '', 'bin': '', 'log': ''}
        self.rules = []                  # custom filter rules
        self.rules_seeded = False
        self.rules_seed_ver = 2          # v2 = '>' throttle-on defaults
        self.last_error = ''
        self._load_store()

    # ---- persistence (desktop; no-ops in web mode) ----
    def _load_store(self):
        if WEB_MODE or not self.store:
            return
        try:
            with open(self.store, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.recent = {k: [p for p in data.get('recent', {}).get(k, []) if isinstance(p, str)][:10]
                           for k in ('xdf', 'bin', 'log')}
            self.dirs = {k: data.get('dirs', {}).get(k, '') for k in ('xdf', 'bin', 'log')}
            rules = data.get('rules', [])
            self.rules = [{'col': int(r['col']), 'op': str(r.get('op', '<')),
                           'value': float(r.get('value', 0))}
                          for r in rules
                          if isinstance(r, dict) and 'col' in r]
            self.rules_seeded = bool(data.get('rules_seeded', False))
            # v1 seeded '<' cut rules; v2 seeds '>' throttle-on rules — retire old defaults
            ver = int(data.get('rules_seed_ver', 1 if self.rules_seeded else 2))
            if (ver < 2 and self.rules and len(self.rules) <= 2
                    and all(r['op'] == '<' and r['value'] in (0.5, 1.0, 2.0)
                            for r in self.rules)):
                self.rules = []
                self.rules_seeded = False
            self.rules_seed_ver = 2
        except (OSError, ValueError, TypeError, KeyError):
            pass

    def save_store(self):
        if WEB_MODE or not self.store:
            return
        try:
            with open(self.store, 'w', encoding='utf-8') as f:
                json.dump({'recent': self.recent, 'dirs': self.dirs,
                           'rules': self.rules, 'rules_seeded': self.rules_seeded,
                           'rules_seed_ver': self.rules_seed_ver},
                          f, indent=1)
        except OSError:
            pass

    MEM_FILE_BUDGET = 64 * 1024 * 1024     # per-session cap for remembered uploads

    def push_recent(self, kind, name):
        if not WEB_MODE:
            name = os.path.abspath(name)
        lst = self.recent.setdefault(kind, [])
        if name in lst:
            lst.remove(name)
        lst.insert(0, name)
        del lst[10:]
        if not WEB_MODE:
            d = os.path.dirname(name)
            if d:
                self.dirs[kind] = d
            self.save_store()

    def remember_file(self, kind, name, data):
        """Web mode: keep uploaded bytes in RAM so recents work within the session."""
        if not WEB_MODE or not data:
            return
        mem = self.mem_files.setdefault(kind, [])
        for i, (n, _) in enumerate(mem):
            if n == name:
                del mem[i]
                break
        mem.insert(0, (name, data))
        kept, total = [], 0
        for n, d in mem:
            if total + len(d) <= self.MEM_FILE_BUDGET:
                kept.append((n, d))
                total += len(d)
        self.mem_files[kind] = kept

    def session_file(self, kind, name):
        """Bytes of a file uploaded earlier in this session, or None."""
        for n, d in self.mem_files.get(kind, []):
            if n == name:
                return d
        return None

    def log(self, msg):
        self.messages.append(msg)
        del self.messages[:-200]


_DEFAULT_STATE = AppState()
_STATE_CTX = contextvars.ContextVar('ve_session_state', default=None)


class _StateProxy:
    """Routes STATE access to the current browser session (web mode) or to the
    single desktop instance when no session context is active."""
    def __getattr__(self, name):
        st = _STATE_CTX.get()
        return getattr(st if st is not None else _DEFAULT_STATE, name)

    def __setattr__(self, name, value):
        st = _STATE_CTX.get()
        setattr(st if st is not None else _DEFAULT_STATE, name, value)


STATE = _StateProxy()


def ds(*events):
    return DatastarResponse([e for e in events])


def msg_events(*extra):
    yield SSE.patch_elements(msglog_frag(), selector='#msglog', mode='inner')
    for e in extra:
        yield e


# -----------------------------------------------------------------------------
# native file dialog: runs in a short-lived SUBPROCESS so the dialog always has
# its own main thread (works identically in windowed/terminal/dev modes).
# -----------------------------------------------------------------------------
def _pick_via_subprocess(kind, initial_dir):
    import subprocess
    import tempfile
    outfile = os.path.join(tempfile.gettempdir(), f"ve_pick_{os.getpid()}_{int(time.time() * 1000)}.json")
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--pick", kind, "--out", outfile]
    else:
        cmd = [sys.executable, os.path.abspath(__file__), "--pick", kind,
               "--out", outfile, "--dir", initial_dir or ""]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        subprocess.run(cmd, timeout=1800, creationflags=flags,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(outfile):
            with open(outfile, "r", encoding="utf-8") as f:
                paths = json.load(f)
            os.remove(outfile)
            return [str(p) for p in paths if p]
    except Exception as e:  # noqa: BLE001
        STATE.log(f"!! File dialog error: {type(e).__name__}: {e}")
    return []


def request_files(kind):
    """Blocking call (worker thread) that opens the native dialog via subprocess."""
    return _pick_via_subprocess(kind, STATE.dirs.get(kind, ""))


def pick_files(kind):
    """Open the native Windows file dialog (runs in the child process's MAIN thread).

    Returns a list of selected paths (empty on cancel)."""
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.lift()
    root.update_idletasks()
    try:
        if kind == "logs":
            paths = filedialog.askopenfilenames(
                parent=root, title="Select CSV datalogs",
                initialdir=STATE.dirs.get("log") or None,
                filetypes=[("CSV logs", "*.csv"), ("All files", "*.*")])
            result = list(paths)
        else:
            p = filedialog.askopenfilename(
                parent=root,
                title="Select XDF definition" if kind == "xdf" else "Select binary",
                initialdir=STATE.dirs.get(kind) or None,
                filetypes=([("XDF definitions", "*.xdf"), ("All files", "*.*")]
                           if kind == "xdf" else
                           [("Binaries", "*.bin"), ("All files", "*.*")]))
            result = [p] if p else []
    finally:
        root.destroy()
    return result


# ----------------------------------------------------------------------------- 
# helpers: loading, validation
# ----------------------------------------------------------------------------- 
def load_xdf(path=None, *, name=None, data=None):
    """Load the definition from disk (desktop) or from in-memory bytes (web)."""
    disp = name or path
    if data is None:
        try:
            data = read_bytes_robust(path)
        except OSError as e:
            STATE.log(f"!! Could not read XDF: {e}")
            return False
    try:
        base, tables = parse_xdf_data(data)
    except Exception as e:  # noqa: BLE001
        STATE.log(f"!! Could not parse XDF: {e}")
        return False
    STATE.xdf_path = disp
    STATE.xdf_base = base
    STATE.xdf_tables = tables
    STATE.ve_tables = find_ve_family(tables)
    STATE.push_recent('xdf', disp)
    STATE.remember_file('xdf', disp, data)
    STATE.log(f"XDF loaded: {os.path.basename(disp)} ({len(tables)} tables, "
              f"base offset 0x{base:X}, {len(STATE.ve_tables)} VE tables)")
    if STATE.bin_path:
        if WEB_MODE and STATE.bin_data is not None:
            load_bin(name=os.path.basename(STATE.bin_path), data=STATE.bin_data)
        elif not WEB_MODE:
            load_bin(STATE.bin_path)     # re-read tables with new definition
    return True


def load_bin(path=None, *, name=None, data=None):
    """Load the binary from disk (desktop) or from in-memory bytes (web)."""
    if not STATE.ve_tables:
        STATE.log("!! Load an XDF with VE tables first.")
        return False
    disp = name or path
    if data is None:
        try:
            data = read_bytes_robust(path)
        except OSError as e:
            STATE.log(f"!! Could not read binary: {e}")
            return False
    maxend = max(td.addr + td.nbytes for td in STATE.ve_tables)
    if maxend > len(data):
        STATE.log(f"!! Binary too small for this XDF (needs 0x{maxend:X}, has 0x{len(data):X}). "
                  "Wrong XDF (64K vs 512K) or wrong file?")
        return False
    STATE.bin_path = disp
    STATE.bin_data = data
    STATE.push_recent('bin', disp)
    STATE.remember_file('bin', disp, data)
    STATE.tuner = Tuner()
    xa = resolve_axis(STATE.ve_tables[0].axes['x'], STATE.xdf_tables)
    ya = resolve_axis(STATE.ve_tables[0].axes['y'], STATE.xdf_tables)
    STATE.tuner.read_tables(data, STATE.ve_tables, xa, ya)
    STATE.analysis_valid = False
    STATE.apply_valid = False
    t = STATE.tuner
    STATE.log(f"Binary loaded: {os.path.basename(disp)} ({len(data)//1024} KB, "
              f"{'big' if t.big else 'little'}-endian, MAP {t.map_ax.min():.0f}-{t.map_ax.max():.0f} kPa, "
              f"RPM {t.rpm_ax.min():.0f}-{t.rpm_ax.max():.0f})")
    return True


def add_log_paths(paths):
    """Desktop: add logs from file paths."""
    return _add_logs([(p.strip().strip('"'), None) for p in paths if p.strip().strip('"')])


def add_log_files(files):
    """Web: add logs from (name, bytes) pairs — nothing touches disk."""
    return _add_logs(list(files))


def _add_logs(items):
    added = rejected = 0
    for disp, data in items:
        if data is None:
            if any(lg.path == os.path.abspath(disp) for lg in STATE.logs):
                continue
        elif any(lg.path == disp for lg in STATE.logs):
            continue
        lf = LogFile(disp) if data is None else LogFile(None, name=disp, data=data)
        if lf.error or not lf.rows:
            STATE.log(f"!! Could not read log {os.path.basename(disp)}: {lf.error or 'no rows'}")
            rejected += 1
            continue
        if STATE.logs and not STATE.logs[0].header_matches(lf):
            STATE.log(f"!! Rejected {os.path.basename(disp)}: headers differ from "
                      f"{os.path.basename(STATE.logs[0].path)} — all logs must share identical columns.")
            rejected += 1
            continue
        STATE.logs.append(lf)
        STATE.push_recent('log', disp)
        STATE.remember_file('log', disp, data or b'')
        STATE.log(f"Log added: {os.path.basename(disp)} ({lf.rows:,} rows, {len(lf.header)} columns)")
        added += 1
    if added:
        STATE.channels = guess_channels(STATE.logs[0].header)
        mi = STATE.channels.get('map')
        STATE.map_scale = guess_map_scale(STATE.logs[0].header, mi)
        STATE.analysis_valid = False
        STATE.apply_valid = False
        _seed_rules()
    return added, rejected


def _seed_rules():
    """First-ever log load: seed a default throttle filter — keep only samples
    where the driver is actually on the gas (pedal/TPS above a threshold)."""
    if STATE.rules_seeded or STATE.rules or not STATE.logs:
        return
    STATE.rules_seeded = True
    from core.logs import _norm
    nh = [_norm(h) for h in STATE.logs[0].header]
    seeds = [('accelerator pedal', '>', 1.0), ('accel pedal', '>', 1.0), ('tps', '>', 0.5)]
    for needle, op, value in seeds:
        idx = next((i for i, n in enumerate(nh) if needle in n), None)
        if idx is not None:
            STATE.rules.append({'col': idx, 'op': op, 'value': value})
            STATE.log(f"Default filter rule: {STATE.logs[0].header[idx].strip()} {op} {value} "
                      "(keep only throttle-on samples — edit in Sample filters)")
            break
    STATE.save_store()


def channels_ok(corr_mode):
    ch = STATE.channels
    need = ('rpm', 'map', 'vetable')
    if not all(ch.get(k) is not None for k in need):
        return False
    if corr_mode in ('afr', 'afr+trim'):
        if ch.get('afrtarget') is None:
            return False
        if ch.get('afr1') is None and ch.get('afr2') is None:
            return False
    else:   # trim-only: lambda disabled
        if not any(ch.get(k) is not None for k in ('stft1', 'stft2', 'ltft1', 'ltft2')):
            return False
    return True


def gather_samples(corr_mode, afr_src):
    n = sum(lg.rows for lg in STATE.logs)
    out = {k: np.empty(n) for k in ('rpm', 'mapkpa', 'avt')}
    optional = {k: np.full(n, np.nan) for k in ('inj', 'clt', 'time', 'afr', 'target', 'trim')}
    ch = STATE.channels
    pos = 0

    def banktrim(lg, r):
        banks = []
        for st, lt in (('stft1', 'ltft1'), ('stft2', 'ltft2')):
            parts = []
            for kk in (st, lt):
                ci = ch.get(kk)
                if ci is None:
                    continue
                arr = lg.col(ci)
                if arr is not None:
                    parts.append(np.nan_to_num(arr[:r]))
            if parts:
                banks.append(sum(parts))
        if not banks:
            return None
        return np.mean(np.vstack(banks), axis=0)

    for lg in STATE.logs:
        r = lg.rows
        sl = slice(pos, pos + r)
        pos += r
        for dst, key in (('rpm', 'rpm'), ('mapkpa', 'map'), ('avt', 'vetable')):
            ci = ch.get(key)
            arr = lg.col(ci) if ci is not None else None
            if arr is None or len(arr) != r:
                return None, f"Column missing in {os.path.basename(lg.path)}"
            out[dst][sl] = arr[:r]
        out['mapkpa'][sl] *= STATE.map_scale

        bt = banktrim(lg, r)
        if bt is not None:
            optional['trim'][sl] = bt

        if corr_mode in ('afr', 'afr+trim'):
            ci = ch.get('afrtarget')
            arr = lg.col(ci) if ci is not None else None
            if arr is None or len(arr) != r:
                return None, "AFR target column missing"
            optional['target'][sl] = arr[:r]
            a1 = a2 = None
            ci = ch.get('afr1')
            if ci is not None:
                a1 = lg.col(ci)
                if a1 is not None:
                    a1 = np.where((a1[:r] > 5) & (a1[:r] < 25), a1[:r], np.nan)
            ci = ch.get('afr2')
            if ci is not None:
                a2 = lg.col(ci)
                if a2 is not None:
                    a2 = np.where((a2[:r] > 5) & (a2[:r] < 25), a2[:r], np.nan)
            if afr_src == 'b1':
                afr = a1 if a1 is not None else a2
            elif afr_src == 'b2':
                afr = a2 if a2 is not None else a1
            else:
                stack = [a for a in (a1, a2) if a is not None]
                if not stack:
                    return None, "No wideband AFR channel available"
                if len(stack) == 2:
                    with np.errstate(invalid='ignore'):
                        afr = np.nanmean(np.vstack(stack), axis=0)
                else:
                    afr = stack[0]
            if afr is None:
                return None, "No wideband AFR channel available"
            optional['afr'][sl] = afr

        for dst, key in (('inj', 'inj'), ('clt', 'clt'), ('time', 'time')):
            ci = ch.get(key)
            arr = lg.col(ci) if ci is not None else None
            if arr is not None:
                optional[dst][sl] = arr[:r]

    out.update(optional)
    tr = out.get('trim')
    if tr is not None and np.allclose(tr, 0):
        out['trim'] = None
    if STATE.units == 'lambda':
        s = STATE.stoich
        out['afr'] = out['afr'] * s
        out['target'] = out['target'] * s
    return out, None


def params_from_signals(sig):
    """Map camelCase client signals to the tuner's snake_case params."""
    raw = sig.get('params') or {}

    def num(key, default):
        try:
            return float(raw.get(key, default))
        except (TypeError, ValueError):
            return default

    def flag(key, default):
        v = raw.get(key, default)
        if isinstance(v, str):
            return v.lower() in ('true', '1', 'on')
        return bool(v)

    p = {
        'rpm_min': num('rpmMin', PARAM_DEFAULTS['rpmMin']),
        'rpm_max': num('rpmMax', PARAM_DEFAULTS['rpmMax']),
        'afr_min': num('afrMin', PARAM_DEFAULTS['afrMin']),
        'afr_max': num('afrMax', PARAM_DEFAULTS['afrMax']),
        'use_clt': flag('useClt', PARAM_DEFAULTS['useClt']),
        'clt_min': num('cltMin', PARAM_DEFAULTS['cltMin']),
        'use_transient': flag('useTrans', PARAM_DEFAULTS['useTrans']),
        'trans_rpm': num('transRpm', PARAM_DEFAULTS['transRpm']),
        'trans_map': num('transMap', PARAM_DEFAULTS['transMap']),
        'strength': num('strength', PARAM_DEFAULTS['strength']),
        'max_change': num('maxChange', PARAM_DEFAULTS['maxChange']),
        'min_samples': num('minSamples', PARAM_DEFAULTS['minSamples']),
        'smoothing': num('smoothing', PARAM_DEFAULTS['smoothing']),
        'blend_tables': flag('blend', PARAM_DEFAULTS['blend']),
        'stoich': num('stoich', PARAM_DEFAULTS['stoich']),
    }
    # resolve custom filter rules to per-sample value arrays
    rules_param = []
    for r in STATE.rules:
        idx = int(r.get('col', -1))
        if not STATE.logs or not (0 <= idx < len(STATE.logs[0].header)):
            continue
        vals = _column_values(idx)
        if vals is None:
            continue
        rules_param.append({'name': STATE.logs[0].header[idx].strip(),
                            'vals': vals, 'op': r.get('op', '<'),
                            'value': float(r.get('value', 0))})
    p['rules'] = rules_param
    p['corr_mode'] = STATE.corr_mode
    return p


def run_analysis(sig):
    p = params_from_signals(sig)
    afr_src = STATE.afr_src
    if not channels_ok(p['corr_mode']):
        missing = [k for k in ('rpm', 'map', 'vetable', 'afrtarget') if STATE.channels.get(k) is None]
        STATE.log(f"!! Channels incomplete for the selected correction mode "
                  f"(missing: {', '.join(missing) or 'wideband/trims'}). "
                  "Fix the dropdowns in section 2 · Datalogs, then Analyze again.")
        return False
    samples, err = gather_samples(p['corr_mode'], afr_src)
    if samples is None:
        STATE.log(f"!! {err}")
        return False
    STATE.tuner.analyze(samples, p)
    STATE.tuner.apply(p)
    STATE.analysis_valid = True
    STATE.apply_valid = True
    st = STATE.tuner.stats
    reasons = ", ".join(f"{k}: {v:,}" for k, v in st['reasons'].items() if v) or "none"
    STATE.log(f"Analyzed {st['used']:,}/{st['total']:,} samples ({p['corr_mode']} mode). "
              f"Excluded: {reasons}")
    if st.get('units_hint'):
        STATE.log(f"!! {st['units_hint']}")
    # sanity check: all samples binned into a single MAP row means the MAP
    # channel or its unit is wrong (e.g. Bar values read as kPa)
    try:
        wsum = None
        for w in STATE.tuner.W:
            r = np.asarray(w).sum(axis=1)
            wsum = r if wsum is None else wsum + r
        if wsum is not None and len(wsum) > 3 and wsum.sum() > 0 and int((wsum > 0).sum()) <= 1:
            STATE.log("!! All samples landed in one MAP row — the MAP channel or its unit "
                      "is wrong. Check the 'Manifold pressure' channel and the MAP In "
                      "(Bar/kPa) setting, then Analyze again.")
    except Exception:  # noqa: BLE001
        pass
    return True


# ----------------------------------------------------------------------------- 
# fragments (server-rendered HTML)
# ----------------------------------------------------------------------------- 
def msglog_frag():
    body = '\n'.join(esc(m) for m in STATE.messages[-40:])
    return f'<pre id="msglog-pre">{body or "ready"}</pre>'


def statusbar_frag():
    bits = []
    if STATE.xdf_path:
        bits.append(f"XDF: {esc(os.path.basename(STATE.xdf_path))}")
    if STATE.bin_path:
        bits.append(f"BIN: {esc(os.path.basename(STATE.bin_path))}")
    if STATE.ve_tables:
        bits.append(f"{len(STATE.ve_tables)} VE tables")
    if STATE.bin_data is not None:
        bits.append("little-endian" if not STATE.tuner.big else "big-endian")
    if STATE.logs:
        bits.append(f"{len(STATE.logs)} log(s)")
    pills = ''.join(f'<span class="pill">{b}</span>' for b in bits) or \
        '<span class="pill dim">load a definition to begin</span>'
    return f'<div class="pills">{pills}</div>'


def definfo_frag():
    if not STATE.xdf_path:
        return '<span class="dim">no definition loaded</span>'
    n = len(STATE.ve_tables)
    info = f"{n} VE tables · base offset 0x{STATE.xdf_base:X}"
    cls = "dim" if n else "bad"
    return f'<span class="{cls}">{esc(os.path.basename(STATE.xdf_path))} — {info}</span>'


def loglist_frag():
    if not STATE.logs:
        return '<div class="dim" style="padding:6px 2px">no logs loaded</div>'
    rows = []
    for i, lg in enumerate(STATE.logs):
        rows.append(
            f'<div class="logrow"><span class="logname" title="{esc(lg.path)}">'
            f'{esc(os.path.basename(lg.path))}</span>'
            f'<span class="dim">{lg.rows:,} rows</span>'
            f'<button class="mini" data-on:click="@post(\'/api/logs/remove/{i}\')">✕</button></div>')
    return ''.join(rows)


def chanmap_frag():
    if not STATE.logs:
        return '<div class="dim" style="padding:4px 2px">channels appear after logs are added</div>'
    header = STATE.logs[0].header
    from core.logs import CHANNEL_LABELS
    cells = []
    for k in CHANNEL_KEYS:
        req = ' *' if k in ('rpm', 'map', 'vetable', 'afrtarget') else ''
        if STATE.corr_mode == 'trim' and k == 'afrtarget':
            req = ''
        opts = ['<option value="-1">— not available —</option>']
        cur = STATE.channels.get(k)
        for i, h in enumerate(header):
            sel = ' selected' if cur == i else ''
            opts.append(f'<option value="{i}"{sel}>{esc(h.strip() or f"col {i}")}</option>')
        cells.append(
            f'<label class="chan">{CHANNEL_LABELS.get(k, k)}{req}</label>'
            f'<select data-bind="channels.{k}" data-on:change="@post(\'/api/channel\')">'
            + ''.join(opts) + '</select>')
    # fingerprint so the server can tell stale-tab channel signals from real ones
    return (f'<div class="changrid">{"".join(cells)}</div>'
            f'<span style="display:none" data-signals="{{&quot;channelsLog&quot;: &quot;{_header_hash()}&quot;}}"></span>')


def corrmode_frag():
    modes = [('afr', 'Wideband AFR vs target (lambda)'),
             ('afr+trim', 'AFR + fuel trims (trims priority)'),
             ('trim', 'Fuel trims only — lambda disabled')]
    btns = ''.join(
        f'<button class="seg{" active" if STATE.corr_mode == m else ""}" '
        f'data-on:click="@post(\'/api/corrmode/{m}\')">{lbl}</button>'
        for m, lbl in modes)
    hint = {
        'afr': 'correction = AFR ÷ AFR target per sample',
        'afr+trim': 'per sample: trim ≠ 0 → correction = 1 + trims/100 (ECU already '
                    'compensating — the wideband reads the corrected mixture); '
                    'trim = 0 → correction = AFR ÷ target (open loop)',
        'trim': 'correction = 1 + (STFT + LTFT)/100 — no wideband or AFR target needed',
    }[STATE.corr_mode]
    return (f'<div class="segrow">{btns}</div>'
            f'<div class="small dim" style="margin-top:5px">{hint}</div>')


def optsrow_frag():
    ub = ''.join(f'<button class="seg mini{" active" if (STATE.map_scale == 100.0) == (u == "bar") else ""}" '
                 f'data-on:click="@post(\'/api/mapunit/{u}\')">{lbl}</button>'
                 for u, lbl in (('bar', 'Bar'), ('kpa', 'kPa')))
    sb = ''.join(f'<button class="seg mini{" active" if STATE.afr_src == s else ""}" '
                 f'data-on:click="@post(\'/api/afrsrc/{s}\')">{lbl}</button>'
                 for s, lbl in (('avg', 'avg banks'), ('b1', 'bank 1'), ('b2', 'bank 2')))
    return (f'<div class="row" style="margin-top:6px"><span class="small dim">MAP in</span>{ub}'
            f'<span class="small dim">AFR source</span>{sb}</div>')


RULE_OPS = [('<', '&lt;'), ('<=', '≤'), ('>', '&gt;'), ('>=', '≥'), ('==', '='), ('!=', '≠')]

# columns offered at the top of the rule dropdown (normalized-name fragments)
COMMON_RULE_COLUMNS = ['accelerator pedal', 'accel pedal', 'tps', 'fuel inj',
                       'engine speed', 'map', 'coolant', 'afr']


def _rule_column_options(header, current):
    """<option> list for a rule's column select: common columns first, rest alphabetical."""
    from core.logs import _norm
    normed = [(i, _norm(h), h.strip() or f"col {i}") for i, h in enumerate(header)]
    common, rest = [], []
    for i, n, disp in normed:
        (common if any(frag in n for frag in COMMON_RULE_COLUMNS) else rest).append((i, disp))
    rest.sort(key=lambda t: t[1].lower())

    def opt(i, disp):
        sel = ' selected' if current == i else ''
        return f'<option value="{i}"{sel}>{esc(disp)}</option>'
    out = [opt(i, d) for i, d in common]
    if rest:
        out.append('<optgroup label="all columns">')
        out += [opt(i, d) for i, d in rest]
        out.append('</optgroup>')
    if current is not None and not (0 <= current < len(header)):
        out.append(f'<option value="{current}" selected>column {current} (missing)</option>')
    return ''.join(out)


def rules_frag():
    """Always visible: existing rules + presets + add button (columns once a log is loaded)."""
    header = STATE.logs[0].header if STATE.logs else []
    rows = []
    for i, r in enumerate(STATE.rules):
        col_ok = 0 <= r['col'] < len(header)
        if col_ok:
            col_html = _rule_column_options(header, r['col'])
        else:
            col_html = f'<option value="{r["col"]}" selected>column {r["col"]} (missing)</option>'
        ops = ''.join(f'<option value="{o}"{" selected" if r["op"] == o else ""}>{lbl}</option>'
                      for o, lbl in RULE_OPS)
        warn = ' title="column not present in the loaded logs"' if not col_ok else ''
        rows.append(
            f'<div class="rulerow"{warn}>'
            f'<select data-bind="rules.{i}.col" data-on:change="@post(\'/api/rule/update/{i}\')">{col_html}</select>'
            f'<select data-bind="rules.{i}.op" data-on:change="@post(\'/api/rule/update/{i}\')">{ops}</select>'
            f'<input type="number" step="0.1" data-bind="rules.{i}.val" value="{r["value"]}" '
            f'data-on:change="@post(\'/api/rule/update/{i}\')">'
            f'<button class="mini" data-on:click="@post(\'/api/rule/remove/{i}\')">✕</button></div>')
    body = ''.join(rows) or '<div class="dim small">no rules yet — all samples kept</div>'
    presets = ''
    if STATE.logs:
        nh = [_norm(h) for h in header]
        chips = []
        for frag_, op, val, lbl in (('accelerator pedal', '>', 1.0, 'pedal &gt; 1'),
                                    ('tps', '>', 0.5, 'TPS &gt; 0.5'),
                                    ('accelerator pedal', '>', 2.0, 'pedal &gt; 2')):
            idx = next((i for i, n in enumerate(nh) if frag_ in n), None)
            if idx is not None:
                chips.append(f'<button class="chip" title="add rule: {lbl.replace("&gt;", ">")}" '
                             f'data-on:click="@post(\'/api/rule/preset/{idx}/{op}/{val}\')">{lbl}</button>')
        presets = ('<span class="dim small">quick add:</span>' + ''.join(chips)) if chips else ''
    add = '<button class="btn mini" data-on:click="@post(\'/api/rule/add\')">+ add rule</button>'
    note = '' if STATE.logs else '<div class="dim small">column choices fill in once a log is loaded</div>'
    return f'{note}<div class="rulelist">{body}</div><div class="ruleadd">{add}{presets}</div>'


def _col_opts_html(inner):
    return inner


def recents_frag():
    out = []
    labels = {'xdf': 'XDF', 'bin': 'binary', 'log': 'log'}
    for kind in ('xdf', 'bin', 'log'):
        lst = STATE.recent.get(kind, [])
        if not lst:
            continue
        chips = []
        for i, p in enumerate(lst[:5]):
            chips.append(
                f'<button class="chip" title="{esc(p)}" '
                f'data-on:click="@post(\'/api/recent/{kind}/{i}\')">'
                f'{esc(os.path.basename(p))}</button>')
        out.append(f'<div class="recentrow"><span class="dim">{labels[kind]}:</span>'
                   f'{"".join(chips)}</div>')
    return f'<div id="recents-in">{"".join(out) or "<span class=\"dim\">no recent files yet</span>"}</div>'


def cell_html(rr, cc, d):
    """One <td> for the VE grid (d = view data dict)."""
    W = d['W'][rr, cc]
    hit = W > 0
    tuned = bool(d['hit'][rr, cc])
    orig = d['orig'][rr, cc]
    new = d['new'][rr, cc]
    delta = (new / orig - 1.0) if orig > 0 else 0.0
    fac = d['fac'][rr, cc]
    mode = d['mode']
    if mode == 'tuned':
        txt = f"{new:.3f}"
    elif mode == 'orig':
        txt = f"{orig:.3f}"
    elif mode == 'delta':
        pct = delta * 100
        if tuned:
            # show tiny changes honestly instead of rounding them to "+0.0"
            txt = f"{pct:+.2f}" if tuned and 0.0 < abs(pct) < 0.05 else f"{pct:+.1f}"
        else:
            txt = "·" if not hit else "0.0"
    elif mode == 'factor':
        txt = f"{fac:.3f}" if hit else "·"
    else:
        txt = f"{W:.0f}" if hit else "·"
    if mode == 'samples':
        if hit:
            t = min(1.0, W / max(d['wmax'], 1e-9))
            bg = css(lerp(HEAT_NONE, HEAT_COUNT, 0.38 + 0.62 * t))
            fg = '#eaf5ec'
        else:
            bg, fg = css(HEAT_NONE), '#8b94a1'
    else:
        bg = css(heat_bg(delta if tuned else 0.0, d['vmax']))
        fg = '#ffffff' if tuned or hit else '#8b94a1'
    tip = (f"MAP {d['map_ax'][rr]:.0f} kPa @ {int(d['rpm_ax'][cc])} rpm\n"
           f"original: {orig:.3f}\nnew: {new:.3f} ({delta * 100:+.1f}%)\n"
           f"factor: {fac:.3f}\nsamples: {W:.1f}")
    return (f'<td style="background:{bg};color:{fg}" title="{esc(tip)}">{txt}</td>')


def _fmt_val(v):
    a = abs(v)
    if a >= 1000:
        return f"{v:.0f}"
    if a >= 100:
        return f"{v:.1f}"
    if a >= 10:
        return f"{v:.2f}"
    return f"{v:.3f}"


def param_grid_frag(t):
    """16x16 grid of the weighted per-cell mean of the selected log parameter."""
    pa = _param_arrays()
    if pa is None:
        return '<div class="dim" style="padding:10px">parameter data unavailable — run Analyze first</div>'
    mean, weight, label = pa
    tun = STATE.tuner
    m = mean[t]
    w = weight[t]
    hit = w > 0
    if hit.any():
        vals = m[hit]
        vmin, vmax = float(vals.min()), float(vals.max())
    else:
        vmin = vmax = 0.0
    cols = ''.join(f'<th>{int(v)}</th>' for v in tun.rpm_ax)
    rows = []
    for rr in range(len(tun.map_ax)):
        cells = []
        for cc in range(len(tun.rpm_ax)):
            if hit[rr, cc]:
                v = m[rr, cc]
                txt = _fmt_val(v)
                rng = (vmax - vmin) or 1.0
                tt = min(1.0, (v - vmin) / rng)
                bg = css(lerp(HEAT_NONE, HEAT_COUNT, 0.3 + 0.7 * tt))
                fg = '#eaf5ec'
                tip = f"mean {txt}\nsamples: {w[rr, cc]:.1f}"
            else:
                txt, bg, fg, tip = '·', css(HEAT_NONE), '#8b94a1', 'no data'
            cells.append(f'<td style="background:{bg};color:{fg}" title="{esc(tip)}">{txt}</td>')
        rows.append(f'<tr><th>{tun.map_ax[rr]:.0f}</th>{"".join(cells)}</tr>')
    n = int(weight[t].sum())
    return (f'<div class="tabhead">per-cell mean of “{esc(label)}” · {n:.0f} samples in table VE {t + 1}'
            f' · range {fmt_val_char(vmin, vmax)}</div>'
            f'<table class="ve"><tr><th class="corner">kPa\\rpm</th>{cols}</tr>{"".join(rows)}</table>')


def fmt_val_char(vmin, vmax):
    return f"{_fmt_val(vmin)} … {_fmt_val(vmax)}"


def param_select_frag():
    """Dropdown of viewable per-cell parameters (derived + every CSV column)."""
    cur = STATE.param_view
    cur_key = ''
    if cur == 'afr':
        cur_key = 'afr'
    elif cur == 'target':
        cur_key = 'target'
    elif cur == 'lambda':
        cur_key = 'lambda'
    elif isinstance(cur, tuple) and cur[0] == 'col':
        cur_key = f'col:{cur[1]}'
    opts = ['<option value=""' + (' selected' if not cur_key else '') + '>VE tune views…</option>',
            '<option value="afr"' + (' selected' if cur_key == 'afr' else '') + '>Measured AFR</option>',
            '<option value="target"' + (' selected' if cur_key == 'target' else '') + '>AFR target</option>',
            '<option value="lambda"' + (' selected' if cur_key == 'lambda' else '') + '>Lambda</option>']
    if STATE.logs:
        header = STATE.logs[0].header
        opts.append('<option disabled>── log columns ──</option>')
        for i, h in enumerate(header):
            nm = h.strip() or f"col {i}"
            opts.append(f'<option value="col:{i}"'
                        + (' selected' if cur_key == f'col:{i}' else '') + f'>{esc(nm)}</option>')
    return ('<select data-bind="paramSel" data-on:change="@post(\'/api/paramview\')" '
            'title="Show the weighted per-cell mean of this parameter on the current MAP × RPM grid">'
            + ''.join(opts) + '</select>')


def table_frag(t):
    """The 16x16 grid page for table index t."""
    tun = STATE.tuner
    td = tun.tables[t]
    facs = tun.factors(STATE._last_smoothing)
    hit = tun.W[t] >= STATE._last_min_samples
    d = {'rpm_ax': tun.rpm_ax, 'map_ax': tun.map_ax,
         'orig': tun.orig[t], 'new': tun.new[t], 'fac': facs[t],
         'W': tun.W[t], 'hit': hit,
         'vmax': STATE._last_max_change / 100.0,
         'wmax': max(float(tun.W[t].max()), 1.0),
         'mode': STATE.view_mode}
    cols = ''.join(f'<th>{int(v)}</th>' for v in tun.rpm_ax)
    rows = []
    for rr in range(len(tun.map_ax)):
        cells = ''.join(cell_html(rr, cc, d) for cc in range(len(tun.rpm_ax)))
        rows.append(f'<tr><th>{tun.map_ax[rr]:.0f}</th>{cells}</tr>')
    ts = tun.table_stats[t]
    short = td.description.split(' - ')[-1].strip() if td.description else td.title
    head = (f"{esc(short)} · {ts['samples']:.0f} samples · {ts['tuned']}/{td.rows*td.cols} "
            f"cells tuned · Δ {ts['dmin']*100:+.1f}…{ts['dmax']*100:+.1f}%")
    return (f'<div class="tabhead">{head}</div>'
            f'<table class="ve"><tr><th class="corner">kPa\\rpm</th>{cols}</tr>{"".join(rows)}</table>')


def overview_frag():
    tun = STATE.tuner
    st = tun.stats
    mode_txt = {'afr': 'wideband AFR vs target',
                'afr+trim': 'wideband AFR + fuel trims (trims priority — AFR where trims are 0)',
                'trim': 'fuel trims only (lambda disabled)'}[STATE.corr_mode]
    if STATE.corr_mode != 'trim':
        mode_txt += f" · wideband logged in {'lambda (stoich ' + format(STATE.stoich, '.2f') + ')' if STATE.units == 'lambda' else 'AFR'}"
    reasons = " · ".join(f"{esc(k)}: {v:,}" for k, v in st['reasons'].items() if v) or "none"
    rows = []
    for t, td in enumerate(tun.tables):
        ts = tun.table_stats[t]
        warn = ' class="warn"' if ts['samples'] == 0 else ''
        rows.append(
            f'<tr{warn}><td>VE {t+1}</td><td class="desc">{esc(td.description or "—")}</td>'
            f'<td>{ts["samples"]:.0f}</td><td>{ts["hit"]}</td><td>{ts["tuned"]}</td>'
            f'<td>{ts["fac_mean"]:.3f}</td>'
            f'<td>{ts["dmin"]*100:+.0f}…{ts["dmax"]*100:+.0f}%</td></tr>')
    empty = [f"VE {t+1}" for t, ts in enumerate(tun.table_stats) if ts['samples'] == 0]
    empty_txt = (f'No log coverage: {", ".join(empty)} — drive more / different VANOS conditions.'
                 if empty else 'All tables have log coverage.')
    return f"""
<div class="card">
  <b>{st['used']:,}</b> of {st['total']:,} log samples used
  ({100.0 * st['used'] / max(st['total'], 1):.1f}%) · correction source: {esc(mode_txt)}<br>
  <span class="dim">excluded — {reasons}</span>
</div>
<table class="stats">
<tr><th>Table</th><th>Description</th><th>Samples</th><th>Cells w/ data</th>
<th>Cells tuned</th><th>Mean factor</th><th>Δ range</th></tr>
{''.join(rows)}
</table>
<div class="card dim">{esc(empty_txt)}</div>
<div class="card">
  <b>3D histogram — VANOS table × MAP × RPM</b><br>
  <span class="dim">Each sample is attributed by the active VE table channel (fractional values
  blend between adjacent tables, matching the ECU's interpolation across valve-overlap steps)
  and spread bilinearly over the four surrounding MAP × RPM cells — a weighted 3D histogram.
  Per-cell factor = weighted mean of the correction source; new VE = original ×
  (1 + strength × (factor − 1)), clamped to ± max change and table limits.
  Red = was lean (fuel added), blue = was rich (fuel removed). Iterate until |Δ| &lt; ~3%.</span>
  <div class="small dim" style="margin-top:6px">
  VE only scales the modelled load from the MAF/intake model — if that model is off, VE
  corrections will look inconsistent no matter what. Injection temperature corrections also skew
  resulting VE if you tuned at a different IAT (strongest near idle) — use the Parameter dropdown
  (intake air temp) to spot those cells.</div>
  <div class="mono dim">RPM: {' '.join(f'{int(v)}' for v in tun.rpm_ax)}<br>
  MAP kPa: {' '.join(f'{v:.0f}' for v in tun.map_ax)}</div>
</div>"""


def main_frag(sig=None):
    """Results area: overview pinned on top, then a tab row (Show all / VE 1..N)
    and the selected table(s) underneath."""
    if not STATE.analysis_valid:
        err = f'<div class="placeholder"><div>{esc(STATE.last_error)}<br><br>No analysis yet — add a definition, binary and logs, then press <b>Analyze</b>.</div></div>' \
            if STATE.last_error else \
            '<div class="placeholder">No analysis yet — add a definition, binary and logs, ' \
            'then press <b>Analyze</b>.</div>'
        return err
    tun = STATE.tuner
    t = STATE.show_table                      # 0 = show all, 1..N = single VE table

    # ---- toolbar: view mode + parameter + legend (applies to all tabs) ----
    vbtns = ''.join(
        f'<button class="seg mini{" active" if (STATE.view_mode == m and STATE.param_view is None) else ""}" '
        f'data-on:click="@post(\'/api/viewmode/{i}\')">{lbl}</button>'
        for i, (m, lbl) in enumerate(zip(VIEW_MODES,
                                         ('Tuned VE', 'Original', 'Δ %', 'Factor', 'Samples'))))
    if STATE.param_view == 'samples' or (STATE.param_view is None and STATE.view_mode == 'samples'):
        wmax = max(float(tun.W.max() if tun.W.size else 1.0), 1.0)
        legend = (f'<div class="legendbar"><span>0</span>'
                  f'<div class="grad grad-count"></div><span>{wmax:.0f}</span></div>')
    elif STATE.param_view is not None:
        legend = ''
    else:
        legend = (f'<div class="legendbar"><span>-{STATE._last_max_change:.0f}%</span>'
                  f'<div class="grad"></div><span>+{STATE._last_max_change:.0f}%</span></div>')
    toolbar = (f'<div class="toolbar"><span class="dim">View:</span><span class="segrow">{vbtns}</span>'
               f'<span class="dim">Parameter:</span>{param_select_frag()}'
               f'{legend}</div>')

    # ---- tab row: Show all + VE 1..N ----
    tabs = [f'<button class="tabnav{" active" if t == 0 else ""}" id="taball" '
            f'data-on:click="@post(\'/api/view/0\')">Show all</button>']
    for i in range(len(tun.tables)):
        active = ' active' if i + 1 == t else ''
        samples = tun.table_stats[i]['samples']
        dot = '' if samples else ' <span class="dim">○</span>'
        tabs.append(f'<button class="tabnav{active}" id="tab{i + 1}" '
                    f'data-on:click="@post(\'/api/view/{i + 1}\')">'
                    f'VE {i + 1}{dot}</button>')

    # ---- content: one table or all stacked ----
    if t == 0:
        body = ''.join(f'<div class="vecard">{table_frag(i)}</div>'
                       for i in range(len(tun.tables)))
        content = f'<div class="alltables">{body}</div>'
    else:
        content = (param_grid_frag(t - 1) if STATE.param_view is not None
                   else table_frag(t - 1))

    overview = (f'<details class="ovw" open><summary>Overview · '
                f'{tun.stats["used"]:,} of {tun.stats["total"]:,} samples used · '
                f'{STATE.corr_mode} mode'
                f'<span class="dim"> — click to collapse</span></summary>'
                f'{overview_frag()}</details>')

    return (f'{overview}{toolbar}'
            f'<div class="tabbar" id="tabbar">{"".join(tabs)}</div>'
            f'<div class="tabcontent">{content}</div>')


# FastAPI app
# -----------------------------------------------------------------------------
app = FastAPI(title=APP_NAME)

# -----------------------------------------------------------------------------
# browser sessions (web mode only): one AppState per browser, held in RAM.
# A `ve_sid` cookie identifies the session; nothing is persisted anywhere —
# closing the tab (or 24 h idle / server restart) discards all state, and
# uploaded ROMs/logs never touch the server's disk.
# -----------------------------------------------------------------------------
if WEB_MODE:
    _SESSIONS = collections.OrderedDict()      # sid -> (AppState, last_used)
    _SESSIONS_LOCK = threading.Lock()
    MAX_SESSIONS = 16
    SESSION_TTL = 24 * 3600

    @app.middleware("http")
    async def _session_middleware(request, call_next):
        sid = request.cookies.get('ve_sid') or ''
        set_cookie = False
        if not sid:
            sid = secrets.token_urlsafe(18)
            set_cookie = True
        now = time.time()
        with _SESSIONS_LOCK:
            for k in [k for k, (_, ts) in _SESSIONS.items() if now - ts > SESSION_TTL]:
                del _SESSIONS[k]
            ent = _SESSIONS.get(sid)
            if ent is None:
                while len(_SESSIONS) >= MAX_SESSIONS:
                    _SESSIONS.popitem(last=False)
                ent = (AppState(), now)
            else:
                ent = (ent[0], now)
            _SESSIONS[sid] = ent
            _SESSIONS.move_to_end(sid)
        tok = _STATE_CTX.set(ent[0])
        try:
            resp = await call_next(request)
        finally:
            _STATE_CTX.reset(tok)
        if set_cookie:
            resp.set_cookie('ve_sid', sid, max_age=7 * 86400,
                            httponly=True, samesite='lax')
        return resp
app.mount("/static", StaticFiles(directory=resource_path("web/static")), name="static")

# view state kept server-side (authoritative)
# (session defaults now live in AppState.__init__ so every browser session
#  starts from the same baseline)


def file_input_rows():
    """Card 1/2 input rows: path fields + native pickers on the desktop,
    browser upload buttons in web mode."""
    if not WEB_MODE:
        return (
            '<div class="row"><input type="text" data-bind="xdfPath" placeholder="or type a .xdf path" spellcheck="false">'
            '<button class="btn" data-on:click="@post(\'/api/pick/xdf\')">📁 Browse…</button>'
            '<button class="btn" data-on:click="@post(\'/api/xdf\')">Load</button></div>',
            '<div class="row"><input type="text" data-bind="binPath" placeholder="or type a .bin path" spellcheck="false">'
            '<button class="btn" data-on:click="@post(\'/api/pick/bin\')">📁 Browse…</button>'
            '<button class="btn" data-on:click="@post(\'/api/bin\')">Load</button></div>',
            '<div class="row"><textarea data-bind="logPath" rows="2" placeholder="or type CSV log path(s) — one per line" spellcheck="false"></textarea>'
            '<button class="btn" data-on:click="@post(\'/api/pick/logs\')">📁 Browse…</button>'
            '<button class="btn" data-on:click="@post(\'/api/logs/add\')">Add</button></div>',
        )
    return (
        '<div class="row"><button class="btn wide" data-on:click="uploadPick(\'xdf\')">📁 Upload .xdf…</button></div>',
        '<div class="row"><button class="btn wide" data-on:click="uploadPick(\'bin\')">📁 Upload .bin…</button></div>',
        '<div class="row"><button class="btn wide" data-on:click="uploadPick(\'log\')">📁 Upload CSV log(s)…</button></div>',
    )


def output_rows_frag():
    """Output buttons: native Save As on the desktop, browser downloads in web mode."""
    if WEB_MODE:
        return ('<div class="row"><a class="btn accent wide" href="/api/download/bin" download '
                'data-class:off="!$hasAnalysis">Download tuned binary</a></div>'
                '<div class="row"><a class="btn wide" href="/api/download/report" '
                'data-class:off="!$hasAnalysis">Download CSV report</a></div>')
    return ('<div class="row"><button class="btn accent wide" data-on:click="@post(\'/api/save/bin\')" '
            'data-attr:disabled="!$hasAnalysis">Save tuned binary…</button></div>'
            '<div class="row"><button class="btn wide" data-on:click="@post(\'/api/save/report\')" '
            'data-attr:disabled="!$hasAnalysis">Save CSV report…</button></div>')


@app.get("/", response_class=HTMLResponse)
def index():
    try:
        with open(resource_path("web/index.html"), "r", encoding="utf-8") as f:
            html = f.read()
    except OSError:
        return HTMLResponse("<h1>web/index.html missing</h1>", status_code=500)
    xdfrow, binrow, logrow = file_input_rows()
    for marker, frag in (("statusbar", statusbar_frag()), ("loglist", loglist_frag()),
                         ("chanmap", chanmap_frag()), ("optsrow", optsrow_frag()),
                         ("corrmode", corrmode_frag()), ("recents", recents_frag()),
                         ("rulesfrag", rules_frag()),
                         ("xdfrow", xdfrow), ("binrow", binrow), ("logrow", logrow),
                         ("outrows", output_rows_frag()),
                         ("main", main_frag()), ("msglog", msglog_frag())):
        html = html.replace(f"<!--SERVER:{marker}-->", frag)
    return HTMLResponse(html)


@app.get("/favicon.ico")
def favicon():
    p = resource_path("web/static/favicon.ico")
    return FileResponse(p) if os.path.exists(p) else Response(status_code=204)


@app.get("/api/recents")
def get_recents():
    return ds(SSE.patch_elements(recents_frag(), selector='#recents', mode='inner'))


def _sig(signals):
    return signals or {}


def _corr(sig):
    return STATE.corr_mode


def _state_signals(sig=None):
    return SSE.patch_signals({
        'hasXdf': bool(STATE.ve_tables),
        'hasBin': STATE.bin_data is not None,
        'hasLogs': bool(STATE.logs),
        'channelsOk': channels_ok(STATE.corr_mode),
        'hasAnalysis': STATE.analysis_valid,
    })


@app.get("/api/debug")
def api_debug():
    out = {"param_view": repr(STATE.param_view),
           "analysis_valid": STATE.analysis_valid,
           "units": STATE.units,
           "rules": STATE.rules}
    tun = STATE.tuner
    out["has_stored"] = getattr(tun, "stored", None) is not None
    if getattr(tun, "stored", None):
        out["mask_sum"] = int(tun.stored["mask"].sum())
    pa = _param_arrays()
    out["param_arrays"] = "None" if pa is None else f"OK label={pa[2]!r} cells={int((pa[1] > 0).sum())}"
    return out


@app.post("/api/recent/{kind}/{i}")
async def recent_load(kind: str, i: int, signals: dict = Depends(read_signals)):
    _sync_channels(_sig(signals))
    lst = STATE.recent.get(kind, [])
    if kind not in ('xdf', 'bin', 'log') or not (0 <= i < len(lst)):
        return ds(*msg_events())
    name = lst[i]
    if WEB_MODE:
        data = STATE.session_file(kind, name)
        if data is None:
            STATE.log(f"!! {name} is no longer in this session's memory — upload it again.")
            return ds(*msg_events(
                SSE.patch_elements(recents_frag(), selector='#recents', mode='inner')))
        if kind == 'xdf':
            load_xdf(name=name, data=data)
        elif kind == 'bin':
            load_bin(name=name, data=data)
        else:
            add_log_files([(name, data)])
        return _def_events()
    path = name
    if not os.path.isfile(path):
        try:
            lst.remove(path)
        except ValueError:
            pass
        STATE.save_store()
        STATE.log(f"!! Recent file no longer exists: {path}")
        return ds(*msg_events(
            SSE.patch_elements(recents_frag(), selector='#recents', mode='inner')))
    if kind == 'xdf':
        load_xdf(path)
    elif kind == 'bin':
        load_bin(path)
    else:
        add_log_paths([path])
    return _def_events()


def _def_events():
    """Patches after any definition/binary/log change."""
    return ds(*msg_events(
        SSE.patch_elements(recents_frag(), selector='#recents', mode='inner'),
        SSE.patch_elements(definfo_frag(), selector='#definfo', mode='inner'),
        SSE.patch_elements(loglist_frag(), selector='#loglist', mode='inner'),
        SSE.patch_elements(chanmap_frag(), selector='#chanmap', mode='inner'),
        SSE.patch_elements(optsrow_frag(), selector='#optsrow', mode='inner'),
        SSE.patch_elements(rules_frag(), selector='#rulesfrag', mode='inner'),
        SSE.patch_elements(statusbar_frag(), selector='#statusbar', mode='inner'),
        _state_signals()))


@app.post("/api/xdf")
async def api_xdf(signals: dict = Depends(read_signals)):
    sig = _sync_channels(_sig(signals))
    path = (sig.get('xdfPath') or '').strip().strip('"')
    if path:
        load_xdf(path)
    return _def_events()


@app.post("/api/bin")
async def api_bin(signals: dict = Depends(read_signals)):
    sig = _sync_channels(_sig(signals))
    path = (sig.get('binPath') or '').strip().strip('"')
    if path:
        load_bin(path)
    return _def_events()


def _logs_events(sig):
    return ds(*msg_events(
        SSE.patch_elements(loglist_frag(), selector='#loglist', mode='inner'),
        SSE.patch_elements(chanmap_frag(), selector='#chanmap', mode='inner'),
        SSE.patch_elements(optsrow_frag(), selector='#optsrow', mode='inner'),
        SSE.patch_elements(rules_frag(), selector='#rulesfrag', mode='inner'),
        SSE.patch_elements(statusbar_frag(), selector='#statusbar', mode='inner'),
        SSE.patch_elements(recents_frag(), selector='#recents', mode='inner'),
        _state_signals(),
        SSE.patch_signals({'logPath': ''})))


@app.post("/api/logs/add")
async def api_logs_add(signals: dict = Depends(read_signals)):
    sig = _sync_channels(_sig(signals))
    raw = sig.get('logPath') or ''
    add_log_paths(raw.replace(';', '\n').split('\n'))
    return _logs_events(sig)


@app.post("/api/pick/{kind}")
async def api_pick(kind: str, signals: dict = Depends(read_signals)):
    """Open the native Windows file dialog; load whatever the user picks."""
    import asyncio
    sig = _sync_channels(_sig(signals))
    if WEB_MODE:
        STATE.log("Native dialogs are unavailable in the web version — "
                  "use the Upload buttons or drag & drop.")
        return ds(*msg_events())
    if kind not in ('xdf', 'bin', 'logs'):
        return ds(*msg_events())
    paths = await asyncio.to_thread(request_files, 'logs' if kind == 'logs' else kind)
    if not paths:
        STATE.log("File dialog cancelled.")
        return ds(*msg_events())
    if kind == 'xdf':
        load_xdf(paths[0])
        return ds(*msg_events(
            SSE.patch_elements(definfo_frag(), selector='#definfo', mode='inner'),
            SSE.patch_elements(statusbar_frag(), selector='#statusbar', mode='inner'),
            SSE.patch_elements(recents_frag(), selector='#recents', mode='inner'),
            _state_signals(),
            SSE.patch_signals({'xdfPath': paths[0]})))
    if kind == 'bin':
        load_bin(paths[0])
        return ds(*msg_events(
            SSE.patch_elements(definfo_frag(), selector='#definfo', mode='inner'),
            SSE.patch_elements(statusbar_frag(), selector='#statusbar', mode='inner'),
            SSE.patch_elements(recents_frag(), selector='#recents', mode='inner'),
            _state_signals(),
            SSE.patch_signals({'binPath': paths[0]})))
    add_log_paths(paths)
    return ds(*msg_events(
        SSE.patch_elements(loglist_frag(), selector='#loglist', mode='inner'),
        SSE.patch_elements(chanmap_frag(), selector='#chanmap', mode='inner'),
        SSE.patch_elements(optsrow_frag(), selector='#optsrow', mode='inner'),
        SSE.patch_elements(statusbar_frag(), selector='#statusbar', mode='inner'),
        SSE.patch_elements(recents_frag(), selector='#recents', mode='inner'),
        _state_signals(),
        SSE.patch_signals({'logPath': ''})))


@app.post("/api/refresh")
async def api_refresh(signals: dict = Depends(read_signals)):
    """Re-render everything the server knows (used after drag & drop uploads)."""
    _sync_channels(_sig(signals))
    return ds(*msg_events(
        SSE.patch_elements(definfo_frag(), selector='#definfo', mode='inner'),
        SSE.patch_elements(loglist_frag(), selector='#loglist', mode='inner'),
        SSE.patch_elements(chanmap_frag(), selector='#chanmap', mode='inner'),
        SSE.patch_elements(optsrow_frag(), selector='#optsrow', mode='inner'),
        SSE.patch_elements(statusbar_frag(), selector='#statusbar', mode='inner'),
        SSE.patch_elements(recents_frag(), selector='#recents', mode='inner'),
        _state_signals()))


@app.post("/api/upload")
async def api_upload(request: Request):
    """Drag & drop / upload target. Web mode keeps everything in RAM; the
    desktop version also saves a copy into the uploads folder."""
    form = await request.form()
    disk = {'xdf': [], 'bin': [], 'log': []}     # desktop: paths
    mem = {'xdf': [], 'bin': [], 'log': []}      # web: (name, bytes)
    total = 0
    for _key, field in form.multi_items():
        name = os.path.basename(getattr(field, 'filename', '') or '')
        data = await field.read()
        total += len(data)
        if total > MAX_UPLOAD:
            STATE.log(f"!! Upload rejected — larger than {MAX_UPLOAD // (1024 * 1024)} MB limit.")
            return Response(f"upload larger than {MAX_UPLOAD // (1024 * 1024)} MB limit",
                            status_code=413)
        if not name or not data:
            continue
        ext = os.path.splitext(name)[1].lower()
        kind = {'.xdf': 'xdf', '.bin': 'bin', '.csv': 'log'}.get(ext)
        if kind is None:
            STATE.log(f"!! Dropped file ignored (unknown type): {name}")
            continue
        if WEB_MODE:
            mem[kind].append((name, data))
        else:
            uploads_dir = os.path.join(os.path.dirname(STATE.store), 'uploads')
            os.makedirs(uploads_dir, exist_ok=True)
            safe = re.sub(r'[^A-Za-z0-9 ._()-]', '_', name)
            path = os.path.join(uploads_dir, safe)
            write_bytes_robust(path, data)
            disk[kind].append(path)
    for p in disk['xdf']:
        load_xdf(p)
    for p in disk['bin']:
        load_bin(p)
    if disk['log']:
        add_log_paths(disk['log'])
    for name, data in mem['xdf']:
        load_xdf(name=name, data=data)
    for name, data in mem['bin']:
        load_bin(name=name, data=data)
    if mem['log']:
        add_log_files(mem['log'])
    n = sum(len(v) for v in list(disk.values()) + list(mem.values()))
    if n:
        STATE.log(f"Received {n} dropped file(s).")
    return ds(*msg_events(
        SSE.patch_elements(definfo_frag(), selector='#definfo', mode='inner'),
        SSE.patch_elements(loglist_frag(), selector='#loglist', mode='inner'),
        SSE.patch_elements(chanmap_frag(), selector='#chanmap', mode='inner'),
        SSE.patch_elements(optsrow_frag(), selector='#optsrow', mode='inner'),
        SSE.patch_elements(statusbar_frag(), selector='#statusbar', mode='inner'),
        SSE.patch_elements(recents_frag(), selector='#recents', mode='inner'),
        _state_signals()))


@app.post("/api/logs/remove/{i}")
async def api_logs_remove(i: int, signals: dict = Depends(read_signals)):
    sig = _sync_channels(_sig(signals))
    if 0 <= i < len(STATE.logs):
        STATE.logs.pop(i)
        if STATE.logs:
            STATE.channels = guess_channels(STATE.logs[0].header)
        else:
            STATE.channels = {}
        STATE.analysis_valid = False
        STATE.apply_valid = False
        STATE.log("Log removed.")
    return ds(*msg_events(
        SSE.patch_elements(loglist_frag(), selector='#loglist', mode='inner'),
        SSE.patch_elements(chanmap_frag(), selector='#chanmap', mode='inner'),
        _state_signals()))


def _header_hash():
    """Short fingerprint of the current log's column layout."""
    if not STATE.logs:
        return ''
    import hashlib
    return hashlib.md5('\x00'.join(h.strip() for h in STATE.logs[0].header)
                       .encode('utf-8', 'replace')).hexdigest()[:8]


def _sync_channels(sig):
    """Merge channel selects from client signals into STATE.

    Guards (a stale tab or a half-bound page must never wipe or corrupt the
    server-side channel detection):
      * the client must send the matching `channelsLog` fingerprint — anything
        else (old page, different log) is ignored entirely;
      * empty / non-numeric values are ignored instead of resetting to None;
      * column indices outside the current header are ignored."""
    if sig.get('channelsLog') != _header_hash():
        return sig
    ch = sig.get('channels') or {}
    header_len = len(STATE.logs[0].header) if STATE.logs else 0
    for k in CHANNEL_KEYS:
        if k not in ch:
            continue
        v = ch.get(k)
        if v is None or v == '':
            continue
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if iv != -1 and (iv < 0 or iv >= header_len):
            continue
        STATE.channels[k] = iv if iv >= 0 else None
    return sig


@app.post("/api/channel")
async def api_channel(signals: dict = Depends(read_signals)):
    _sync_channels(_sig(signals))
    STATE.analysis_valid = False
    STATE.apply_valid = False
    return ds(*msg_events(
        SSE.patch_elements(statusbar_frag(), selector='#statusbar', mode='inner'),
        _state_signals()))


@app.post("/api/corrmode/{m}")
async def api_corrmode(m: str, signals: dict = Depends(read_signals)):
    _sync_channels(_sig(signals))
    if m in ('afr', 'afr+trim', 'trim'):
        STATE.corr_mode = m
        STATE.analysis_valid = False
        STATE.apply_valid = False
    return ds(*msg_events(
        SSE.patch_elements(corrmode_frag(), selector='#corrmode', mode='inner'),
        SSE.patch_elements(chanmap_frag(), selector='#chanmap', mode='inner'),
        _state_signals()))


@app.post("/api/afrsrc/{s}")
async def api_afrsrc(s: str, signals: dict = Depends(read_signals)):
    _sync_channels(_sig(signals))
    if s in ('avg', 'b1', 'b2'):
        STATE.afr_src = s
        if STATE.analysis_valid:
            _run_and_bookmark(_sig(signals))
    return _main_events()


@app.post("/api/mapunit/{u}")
async def api_mapunit(u: str, signals: dict = Depends(read_signals)):
    _sync_channels(_sig(signals))
    if u in ('bar', 'kpa'):
        STATE.map_scale = 100.0 if u == 'bar' else 1.0
        STATE.analysis_valid = False
        STATE.apply_valid = False
    return ds(*msg_events(
        SSE.patch_elements(optsrow_frag(), selector='#optsrow', mode='inner'),
        _state_signals()))


@app.post("/api/rule/add")
async def api_rule_add(signals: dict = Depends(read_signals)):
    _sync_channels(_sig(signals))
    if STATE.logs and len(STATE.rules) < 12:
        header = STATE.logs[0].header
        default_col = next((r['col'] for r in STATE.rules
                            if 0 <= r['col'] < len(header)), 0)
        STATE.rules.append({'col': default_col, 'op': '<', 'value': 1.0})
        STATE.analysis_valid = False
        STATE.apply_valid = False
        STATE.save_store()
    return ds(*msg_events(
        SSE.patch_elements(rules_frag(), selector='#rulesfrag', mode='inner'),
        _state_signals()))


@app.post("/api/rule/preset/{col}/{op}/{value}")
async def api_rule_preset(col: int, op: str, value: float,
                          signals: dict = Depends(read_signals)):
    """Quick-add a rule from a preset chip (e.g. accelerator pedal < 1)."""
    _sync_channels(_sig(signals))
    if op in ('<', '<=', '>', '>=', '==', '!=') and len(STATE.rules) < 12:
        STATE.rules.append({'col': int(col), 'op': op, 'value': float(value)})
        STATE.analysis_valid = False
        STATE.apply_valid = False
        STATE.save_store()
    return ds(*msg_events(
        SSE.patch_elements(rules_frag(), selector='#rulesfrag', mode='inner'),
        _state_signals()))


@app.post("/api/rule/update/{i}")
async def api_rule_update(i: int, signals: dict = Depends(read_signals)):
    sig = _sig(signals)
    rules_sig = sig.get('rules') or {}
    entry = rules_sig.get(str(i)) or rules_sig.get(i)
    if 0 <= i < len(STATE.rules) and isinstance(entry, dict):
        try:
            STATE.rules[i]['col'] = int(entry.get('col', STATE.rules[i]['col']))
        except (TypeError, ValueError):
            pass
        op = str(entry.get('op', STATE.rules[i]['op']))
        if op in ('<', '<=', '>', '>=', '==', '!='):
            STATE.rules[i]['op'] = op
        try:
            STATE.rules[i]['value'] = float(entry.get('val', STATE.rules[i]['value']))
        except (TypeError, ValueError):
            pass
        STATE.analysis_valid = False
        STATE.apply_valid = False
        STATE.save_store()
    return ds(*msg_events(
        SSE.patch_elements(rules_frag(), selector='#rulesfrag', mode='inner'),
        _state_signals()))


@app.post("/api/rule/remove/{i}")
async def api_rule_remove(i: int, signals: dict = Depends(read_signals)):
    _sync_channels(_sig(signals))
    if 0 <= i < len(STATE.rules):
        STATE.rules.pop(i)
        STATE.analysis_valid = False
        STATE.apply_valid = False
        STATE.save_store()
    return ds(*msg_events(
        SSE.patch_elements(rules_frag(), selector='#rulesfrag', mode='inner'),
        _state_signals()))


@app.post("/api/params")
async def api_params(signals: dict = Depends(read_signals)):
    sig = _sync_channels(_sig(signals))
    if STATE.analysis_valid:
        _run_and_bookmark(sig)
    return _main_events()


def _run_and_bookmark(sig):
    ok = run_analysis(sig)
    if ok:
        p = params_from_signals(sig)
        STATE._last_smoothing = p['smoothing']
        STATE._last_min_samples = p['min_samples']
        STATE._last_max_change = p['max_change']
    return ok


def _main_events(sig=None):
    return ds(*msg_events(
        SSE.patch_elements(main_frag(), selector='#main', mode='inner'),
        SSE.patch_elements(statusbar_frag(), selector='#statusbar', mode='inner'),
        _state_signals()))


@app.post("/api/analyze")
async def api_analyze(signals: dict = Depends(read_signals)):
    sig = _sync_channels(_sig(signals))
    if STATE.ve_tables and STATE.bin_data is not None and STATE.logs:
        ok = _run_and_bookmark(sig)
        STATE.last_error = ('' if ok else
                            'Analyze failed — check the message log (bottom) for the reason. '
                            'Most common causes: incomplete channel mapping in section 2, '
                            'or the wrong MAP unit (Bar vs kPa).')
    return _main_events()


@app.post("/api/view/{t}")
async def api_view(t: int, signals: dict = Depends(read_signals)):
    _sync_channels(_sig(signals))
    STATE.show_table = max(0, t)
    return _main_events()


@app.post("/api/viewmode/{i}")
async def api_viewmode(i: int, signals: dict = Depends(read_signals)):
    _sync_channels(_sig(signals))
    try:
        STATE.view_mode = VIEW_MODES[i]
        STATE.param_view = None
    except (TypeError, ValueError, IndexError):
        STATE.view_mode = 'tuned'
    return _main_events()


@app.post("/api/units/{u}")
async def api_units(u: str, signals: dict = Depends(read_signals)):
    sig = _sync_channels(_sig(signals))
    p = params_from_signals(sig)
    if u in ('afr', 'lambda'):
        STATE.units = u
        STATE.stoich = max(11.0, min(20.0, p['stoich']))
        STATE.analysis_valid = False
        STATE.apply_valid = False
    return ds(*msg_events(
        SSE.patch_elements(corrmode_frag(), selector='#corrmode', mode='inner'),
        SSE.patch_elements(optsrow_frag(), selector='#optsrow', mode='inner'),
        SSE.patch_signals({'units': STATE.units, 'stoich': STATE.stoich}),
        _state_signals()))


@app.post("/api/stoich")
async def api_stoich(signals: dict = Depends(read_signals)):
    sig = _sync_channels(_sig(signals))
    p = params_from_signals(sig)
    STATE.stoich = max(11.0, min(20.0, p['stoich']))
    if STATE.units == 'lambda' and STATE.analysis_valid:
        _run_and_bookmark(sig)
    return ds(*msg_events())


@app.post("/api/paramview")
async def api_paramview(signals: dict = Depends(read_signals)):
    sig = _sync_channels(_sig(signals))
    key = (sig.get('paramSel') or '').strip()
    if key in ('', 'afr', 'target', 'lambda'):
        STATE.param_view = key or None
    elif key.startswith('col:'):
        try:
            idx = int(key.split(':')[1])
            STATE.param_view = ('col', idx) if 0 <= idx < len(STATE.logs[0].header) else None
        except (TypeError, ValueError):
            STATE.param_view = None
    return _main_events()


def _column_values(idx):
    """Concatenated raw values of CSV column idx across all logs."""
    parts = []
    for lg in STATE.logs:
        col = lg.data.get(idx)
        if col is not None:
            parts.append(col)
    if not parts:
        return None
    return np.concatenate(parts)


def _param_arrays():
    """Per-cell (mean, weight, label) for the selected parameter view, or None."""
    tun = STATE.tuner
    if not STATE.analysis_valid or getattr(tun, 'stored', None) is None:
        return None
    key = STATE.param_view
    mask = tun.stored['mask']
    if key == 'afr':
        vals = tun.stored['afr']
        label = 'measured AFR' + ('' if STATE.units == 'afr' else ' (from lambda)')
    elif key == 'target':
        vals = tun.stored['target']
        label = 'AFR target'
    elif key == 'lambda':
        base = tun.stored['afr']
        vals = base / STATE.stoich if base is not None else None
        label = f"lambda (λ = {STATE.stoich:.2f} AFR)"
    elif isinstance(key, tuple) and key[0] == 'col':
        raw = _column_values(key[1])
        if raw is None:
            return None
        vals = raw[mask]
        label = STATE.logs[0].header[key[1]].strip() or f"column {key[1]}"
    else:
        return None
    if vals is None:
        return None
    mean, weight = tun.param_means(vals)
    return mean, weight, label


@app.post("/api/revert")
async def api_revert(signals: dict = Depends(read_signals)):
    _sync_channels(_sig(signals))
    if STATE.analysis_valid:
        for t in range(len(STATE.tuner.tables)):
            STATE.tuner.new_raw[t] = STATE.tuner.orig_raw[t].copy()
            STATE.tuner.new[t] = STATE.tuner.orig[t].copy()
        STATE.log("All tables reverted to original values.")
    return _main_events()


def _bin_name():
    return os.path.splitext(os.path.basename(STATE.bin_path or "tuned.bin"))[0] + "_VEtuned.bin"


def _report_name():
    return os.path.splitext(os.path.basename(STATE.bin_path or "report"))[0] + "_VEtune_report.csv"


def _report_csv():
    """Full CSV change report (all cells of all VE tables) as text."""
    tun = STATE.tuner
    facs = tun.factors(STATE._last_smoothing)
    buf = io.StringIO(newline='')
    wr = csv.writer(buf)
    wr.writerow(["table", "map_kpa", "rpm", "original", "new", "delta_pct",
                 "raw_original", "raw_new", "eff_samples", "corr_factor", "tuned"])
    for t, td in enumerate(tun.tables):
        for rr in range(td.rows):
            for cc in range(td.cols):
                old = tun.orig[t][rr, cc]
                new = tun.new[t][rr, cc]
                wr.writerow([
                    td.title, f"{tun.map_ax[rr]:.1f}", int(tun.rpm_ax[cc]),
                    f"{old:.4f}", f"{new:.4f}",
                    f"{(new / old - 1) * 100:+.2f}" if old else "0",
                    tun.orig_raw[t][rr, cc], tun.new_raw[t][rr, cc],
                    f"{tun.W[t][rr, cc]:.2f}", f"{facs[t][rr, cc]:.4f}",
                    int(tun.W[t][rr, cc] >= STATE._last_min_samples)])
    return buf.getvalue()


@app.post("/api/save/bin")
async def save_bin(signals: dict = Depends(read_signals)):
    """Native Save As dialog for the tuned binary (desktop app only)."""
    if WEB_MODE:
        STATE.log("Native save is unavailable in the web version — "
                  "use the Download button instead.")
        return ds(*msg_events())
    if not STATE.apply_valid:
        return Response(status_code=404)
    blob = STATE.tuner.write_binary(STATE.bin_data)
    name = _bin_name()
    tmp = os.path.join(os.path.dirname(STATE.store), name)
    write_bytes_robust(tmp, blob)
    target = await asyncio.to_thread(_save_via_subprocess, tmp, name)
    if target:
        STATE.log(f"Tuned binary saved: {target}")
        return ds(*msg_events())
    STATE.log("Save cancelled.")
    return ds(*msg_events())


@app.post("/api/save/report")
async def save_report(signals: dict = Depends(read_signals)):
    """Native Save As dialog for the CSV change report (desktop app only)."""
    if WEB_MODE:
        STATE.log("Native save is unavailable in the web version — "
                  "use the Download button instead.")
        return ds(*msg_events())
    if not STATE.apply_valid:
        return Response(status_code=404)
    name = _report_name()
    tmp = os.path.join(os.path.dirname(STATE.store), name)
    write_bytes_robust(tmp, _report_csv().encode("utf-8-sig"))
    target = await asyncio.to_thread(_save_via_subprocess, tmp, name)
    if target:
        STATE.log(f"Report saved: {target}")
        return ds(*msg_events())
    STATE.log("Save cancelled.")
    return ds(*msg_events())


@app.get("/api/download/bin")
def download_bin():
    """Browser download of the tuned binary (web mode)."""
    if not STATE.apply_valid:
        return Response(status_code=404)
    blob = STATE.tuner.write_binary(STATE.bin_data)
    name = _bin_name()
    STATE.log(f"Tuned binary prepared for download: {name}")
    return Response(content=blob, media_type='application/octet-stream',
                    headers={'Content-Disposition': f"attachment; filename*=UTF-8''{quote(name)}"})


@app.get("/api/download/report")
def download_report():
    """Browser download of the CSV change report (web mode)."""
    if not STATE.apply_valid:
        return Response(status_code=404)
    name = _report_name()
    STATE.log(f"CSV report prepared for download: {name}")
    return Response(content=_report_csv().encode('utf-8-sig'),
                    media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f"attachment; filename*=UTF-8''{quote(name)}"})


# entry point
# -----------------------------------------------------------------------------
def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _serve(port, host=None):
    import uvicorn
    uvicorn.run(app, host=host or os.environ.get('VE_HOST') or '127.0.0.1', port=port,
                log_level="warning", log_config=None)


def _run_pick_child(argv):
    """Child process modes (spawned by the server):
    --pick <kind> --out <file> [--dir <dir>]     : open open-file dialog, write JSON
    --save <src> --out <file> --name <n> [--dir <d>] : open save-as dialog, copy file"""
    if "--save" in argv:
        src = argv[argv.index("--save") + 1]
        out = argv[argv.index("--out") + 1]
        name = argv[argv.index("--name") + 1] if "--name" in argv else "output.bin"
        d = argv[argv.index("--dir") + 1] if "--dir" in argv else ""
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update_idletasks()
        try:
            target = filedialog.asksaveasfilename(
                parent=root, title="Save tuned binary", initialdir=d or None,
                initialfile=name, defaultextension=".bin",
                filetypes=[("Binary", "*.bin"), ("All files", "*.*")])
            if target:
                import shutil
                shutil.copyfile(src, target)
                result = [target]
            else:
                result = []
        finally:
            root.destroy()
    else:
        kind = argv[argv.index("--pick") + 1] if "--pick" in argv else "logs"
        out = argv[argv.index("--out") + 1] if "--out" in argv else ""
        if "--dir" in argv:
            d = argv[argv.index("--dir") + 1]
            if os.path.isdir(d):
                STATE.dirs[kind if kind != "logs" else "log"] = d
        paths = pick_files(kind)
        result = paths
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f)


def _save_via_subprocess(src_path, default_name):
    """Open the native Save As dialog (child process); copies src to the chosen path.
    Returns the chosen path or None on cancel."""
    import subprocess
    import tempfile
    import shutil
    outfile = os.path.join(tempfile.gettempdir(), f"ve_save_{os.getpid()}_{int(time.time() * 1000)}.json")
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--save", src_path, "--out", outfile,
               "--name", default_name, "--dir", STATE.dirs.get("log", "")]
    else:
        cmd = [sys.executable, os.path.abspath(__file__), "--save", src_path,
               "--out", outfile, "--name", default_name,
               "--dir", STATE.dirs.get("log", "")]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        subprocess.run(cmd, timeout=1800, creationflags=flags,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(outfile):
            with open(outfile, "r", encoding="utf-8") as f:
                res = json.load(f)
            os.remove(outfile)
            return res[0] if res else None
    except Exception as e:  # noqa: BLE001
        STATE.log(f"!! Save dialog error: {type(e).__name__}: {e}")
    return None


def main():
    # windowed (frozen) builds have no console: sys.stdout/stderr are None
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
    argv = sys.argv[1:]

    # child mode: native file dialog process (spawned by request_files / save endpoints)
    if "--pick" in argv or "--save" in argv:
        _run_pick_child(argv)
        return

    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else _free_port()
    url = f"http://127.0.0.1:{port}"

    threading.Thread(target=_serve, args=(port,), daemon=True).start()

    # wait until the server accepts connections before opening any UI
    import urllib.request
    for _ in range(100):
        try:
            urllib.request.urlopen(url + "/api/recents", timeout=0.5)
            break
        except Exception:
            time.sleep(0.1)

    mode = "window"
    if "--browser" in argv or os.environ.get("VE_BROWSER"):
        mode = "browser"
    if "--no-ui" in argv or os.environ.get("VE_NO_UI"):
        mode = "server"

    if mode == "window":
        try:
            import webview
            webview.create_window(APP_NAME, url, width=1440, height=920,
                                  min_size=(1100, 700))
            webview.start()          # blocks until the app window is closed
            print("window closed - shutting down")
            os._exit(0)              # the server dies with the window
        except ImportError:
            print("pywebview not installed - falling back to the default browser")
            mode = "browser"
    if mode == "browser":
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
        print("Ctrl+C to stop the server")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        os._exit(0)
    # server-only mode: block forever
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
