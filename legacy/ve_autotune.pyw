#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MS43 VE Auto-Tune
=================
Auto-tunes the MAP/RPM VE tables (valve-overlap steps 1..8) of a speed-density
patched MS43 ROM from CSV datalog AFR error against target AFR.

Workflow:  XDF definition -> binary (.bin) -> CSV log(s) -> analyze -> review -> write tuned .bin

Similar in spirit to ms4x-maploader (TunerPro XDF based binary editing) but focused
on closed-loop VE learning from wideband logs.
"""

import os
import re
import sys
import csv
import io
import json
import time
import xml.etree.ElementTree as ET

import numpy as np

from PyQt5.QtCore import Qt, QTimer, QSettings, QRect
from PyQt5.QtGui import (QColor, QBrush, QFont, QIcon, QPixmap, QPainter, QPen,
                         QLinearGradient, QPainterPath)
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout,
    QLabel, QPushButton, QFileDialog, QListWidget, QListWidgetItem, QComboBox, QCheckBox,
    QSpinBox, QDoubleSpinBox, QSlider, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem,
    QHeaderView, QGroupBox, QTextEdit, QProgressDialog, QMessageBox, QSizePolicy, QFrame,
    QRadioButton, QScrollArea
)

APP_NAME = "MS43 VE Auto-Tune"
APP_VERSION = "1.1.0"

# transient Windows handle states (cloud-sync placeholders, AV scans, dialog locks)
RETRYABLE_WINERRORS = {6, 22, 32, 121, 1237}


def _read_bytes_with(opener, path, tries=5, base_delay=0.15):
    """Read a file, retrying transient Windows handle errors (WinError 6/32/…)."""
    last = None
    for i in range(tries):
        try:
            with opener(path, 'rb') as f:
                return f.read()
        except OSError as e:
            if getattr(e, 'winerror', None) not in RETRYABLE_WINERRORS:
                raise
            last = e
            time.sleep(base_delay * (2 ** i))
    raise last


def _read_bytes_robust(path, **kw):
    return _read_bytes_with(open, path, **kw)


def _write_bytes_robust(path, data, tries=5, base_delay=0.15):
    """Write a file, retrying transient Windows handle errors."""
    last = None
    for i in range(tries):
        try:
            with open(path, 'wb') as f:
                f.write(data)
                f.flush()
            return
        except OSError as e:
            if getattr(e, 'winerror', None) not in RETRYABLE_WINERRORS:
                raise
            last = e
            time.sleep(base_delay * (2 ** i))
    raise last

# ----------------------------------------------------------------------------- 
# palette
# ----------------------------------------------------------------------------- 
C_BG        = "#1a1d21"
C_PANEL     = "#21252b"
C_PANEL2    = "#262b33"
C_BORDER    = "#3a4150"
C_TEXT      = "#d7dde4"
C_TEXT_DIM  = "#8b94a1"
C_ACCENT    = "#2f81f7"
C_ACCENT_HV = "#1f6feb"
C_GOOD      = "#3fb950"
C_WARN      = "#d29922"
C_BAD       = "#f85149"

HEAT_LEAN  = QColor(0xd2, 0x4b, 0x3e)   # needs more fuel (was lean)
HEAT_RICH  = QColor(0x38, 0x8d, 0xdb)   # needs less fuel (was rich)
HEAT_NONE  = QColor(0x21, 0x25, 0x2b)
HEAT_COUNT = QColor(0x2e, 0xa0, 0x43)

MONO = "Consolas"

STYLESHEET = f"""
* {{ font-family: 'Segoe UI'; font-size: 9pt; }}
QMainWindow, QDialog {{ background-color: {C_BG}; }}
QWidget {{ background-color: transparent; color: {C_TEXT}; }}
QGroupBox {{
    background-color: {C_PANEL}; border: 1px solid {C_BORDER};
    border-radius: 6px; margin-top: 12px; padding: 8px 8px 8px 8px;
    font-weight: 600; color: {C_TEXT};
}}
QGroupBox::title {{
    subcontrol-origin: margin; left: 10px; padding: 0 4px;
    color: {C_ACCENT};
}}
QPushButton {{
    background-color: {C_PANEL2}; color: {C_TEXT}; border: 1px solid {C_BORDER};
    border-radius: 4px; padding: 5px 12px;
}}
QPushButton:hover {{ border-color: {C_ACCENT}; color: white; }}
QPushButton:pressed {{ background-color: {C_BORDER}; }}
QPushButton:disabled {{ color: #5a6270; border-color: #2b313a; }}
QPushButton[accent="true"] {{
    background-color: {C_ACCENT}; border: none; color: white; font-weight: 600;
}}
QPushButton[accent="true"]:hover {{ background-color: {C_ACCENT_HV}; }}
QPushButton[accent="true"]:disabled {{ background-color: #26416b; color: #7f97c2; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background-color: {C_PANEL2}; color: {C_TEXT};
    border: 1px solid {C_BORDER}; border-radius: 4px; padding: 3px 6px;
    selection-background-color: {C_ACCENT};
}}
QComboBox QAbstractItemView {{ background-color: {C_PANEL2}; color: {C_TEXT}; }}
QCheckBox {{ spacing: 6px; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 14px; height: 14px; border: 1px solid {C_BORDER};
    border-radius: 3px; background: {C_PANEL2};
}}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {C_ACCENT}; border-color: {C_ACCENT};
}}
QSlider::groove:horizontal {{ background: {C_BORDER}; height: 5px; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {C_ACCENT}; width: 14px; margin: -5px 0; border-radius: 7px;
}}
QListWidget, QTextEdit, QTableWidget {{
    background-color: {C_PANEL2}; color: {C_TEXT};
    border: 1px solid {C_BORDER}; border-radius: 4px;
}}
QListWidget::item {{ padding: 3px 6px; border-radius: 3px; }}
QListWidget::item:selected {{ background-color: {C_ACCENT}; color: white; }}
QTextEdit {{ font-family: {MONO}; font-size: 8.5pt; }}
QTabWidget::pane {{ border: 1px solid {C_BORDER}; border-radius: 4px; top: -1px; }}
QTabBar::tab {{
    background: {C_PANEL}; color: {C_TEXT_DIM}; padding: 6px 14px;
    border: 1px solid {C_BORDER}; border-bottom: none;
    border-top-left-radius: 4px; border-top-right-radius: 4px; margin-right: 2px;
}}
QTabBar::tab:selected {{ background: {C_PANEL2}; color: white; border-bottom: 2px solid {C_ACCENT}; }}
QHeaderView::section {{
    background-color: {C_PANEL}; color: {C_TEXT};
    border: 1px solid {C_BORDER}; padding: 3px 5px; font-weight: 600;
}}
QTableWidget {{ gridline-color: {C_BORDER}; }}
QTableWidget QTableCornerButton::section {{
    background-color: {C_PANEL2}; border: 1px solid {C_BORDER};
}}
QTableWidget::item {{ padding: 0px; }}
QStatusBar {{ background: {C_PANEL}; border-top: 1px solid {C_BORDER}; color: {C_TEXT_DIM}; }}
QSplitter::handle {{ background-color: {C_BORDER}; width: 3px; }}
QScrollBar:vertical {{ background: {C_BG}; width: 11px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {C_BORDER}; border-radius: 4px; min-height: 24px; margin: 2px; }}
QScrollBar::handle:vertical:hover {{ background: {C_ACCENT}; }}
QScrollBar:horizontal {{ background: {C_BG}; height: 11px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {C_BORDER}; border-radius: 4px; min-width: 24px; margin: 2px; }}
QScrollBar::handle:horizontal:hover {{ background: {C_ACCENT}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
"""

# ----------------------------------------------------------------------------- 
# XDF parsing
# ----------------------------------------------------------------------------- 
def _eq_scale(eq):
    """Return multiplicative scale of a TunerPro MATH like '0.5 * X' (None if complex)."""
    if not eq:
        return 1.0
    eq = eq.replace(' ', '')
    m = re.match(r'^([0-9eE.+-]+)\*[Xx]$', eq)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    if eq.upper() in ('X', '1.0*X'):
        return 1.0
    return None


class AxisDef:
    def __init__(self):
        self.addr = None      # file address of breakpoint words
        self.n = 0
        self.scale = 1.0
        self.linkid = None    # uniqueid of shared axis table
        self.labels = None    # static labels fallback

    def read(self, bindata, big):
        if self.labels is not None:
            return np.array(self.labels, dtype=float)
        if self.addr is None or not self.n:
            return None
        dt = np.dtype(('>' if big else '<') + 'u2')
        b = bindata[self.addr:self.addr + 2 * self.n]
        if len(b) < 2 * self.n:
            return None
        return np.frombuffer(b, dtype=dt).astype(np.float64) * self.scale


class TableDef:
    def __init__(self):
        self.title = ''
        self.uniqueid = None
        self.addr = 0
        self.bits = 16
        self.rows = 0
        self.cols = 0
        self.scale = 1.0
        self.vmin = 0.0
        self.vmax = 65535.0
        self.axes = {'x': AxisDef(), 'y': AxisDef()}
        self.description = ''

    @property
    def nbytes(self):
        return (self.bits // 8) * self.rows * self.cols


def parse_xdf(path):
    """Parse an XDF file into (base_offset, {uniqueid: TableDef})."""
    with open(path, 'rb') as f:
        return parse_xdf_data(f.read())


def parse_xdf_data(data):
    """Parse XDF bytes into (base_offset, {uniqueid: TableDef})."""
    root = ET.fromstring(data)
    base = 0
    bo = root.find('.//BASEOFFSET')
    if bo is not None:
        try:
            base = int(bo.get('offset'), 0) - int(bo.get('subtract'), 0)
        except (TypeError, ValueError):
            base = 0

    tables = {}
    for t in root.findall('.//XDFTABLE'):
        title_el = t.find('title')
        title = (title_el.text or '').strip() if title_el is not None else ''
        z = t.find('XDFAXIS[@id="z"]')
        if z is None:
            continue
        ed = z.find('EMBEDDEDDATA')
        if ed is None or ed.get('mmedaddress') is None:
            continue

        td = TableDef()
        td.title = title
        td.uniqueid = t.get('uniqueid')
        td.addr = base + int(ed.get('mmedaddress'), 16)
        td.bits = int(ed.get('mmedelementsizebits', 16))
        td.rows = int(ed.get('mmedrowcount', 1))
        td.cols = int(ed.get('mmedcolcount', 1))

        eq = z.find('MATH')
        sc = _eq_scale(eq.get('equation') if eq is not None else 'X')
        td.scale = sc if sc is not None else 1.0
        for tag, attr in (('min', 'vmin'), ('max', 'vmax')):
            el = z.find(tag)
            if el is not None and el.text:
                try:
                    setattr(td, attr, float(el.text))
                except ValueError:
                    pass

        desc = t.find('description')
        if desc is not None and desc.text:
            td.description = ' '.join(desc.text.split())

        for axid in ('x', 'y'):
            ax = t.find(f'XDFAXIS[@id="{axid}"]')
            if ax is None:
                continue
            adef = td.axes[axid]
            ic = ax.find('indexcount')
            if ic is not None:
                adef.n = int(ic.text)
            ei = ax.find('embedinfo')
            if ei is not None and ei.get('linkobjid'):
                adef.linkid = ei.get('linkobjid')
            aed = ax.find('EMBEDDEDDATA')
            if aed is not None and aed.get('mmedaddress'):
                adef.addr = base + int(aed.get('mmedaddress'), 16)
                # TunerPro shared axes report 8-bit elements; real storage is 16-bit words
                if int(aed.get('mmedelementsizebits', 8)) <= 8 and adef.linkid is None:
                    pass
            am = ax.find('MATH')
            asc = _eq_scale(am.get('equation') if am is not None else 'X')
            adef.scale = asc if asc is not None else 1.0
            if not adef.n:
                adef.n = td.cols if axid == 'x' else td.rows
            labs = ax.findall('LABEL')
            if labs and (adef.linkid is None and adef.addr is None):
                adef.labels = [float(l.get('value')) for l in labs]
        tables[td.uniqueid] = td
    return base, tables


VE_NUM_RE = re.compile(r'(?:^|[^a-z0-9])ve[ _\-]?(\d+)(?:[^a-z0-9]|$)', re.IGNORECASE)


def find_ve_family(tables):
    """Detect the VE table family: square 2D tables whose title contains ve<digits>."""
    fam = []
    for td in tables.values():
        if td.rows != td.cols or td.rows < 4 or td.bits != 16:
            continue
        m = VE_NUM_RE.search(td.title)
        if not m:
            continue
        if not (td.axes['x'].linkid or td.axes['x'].addr or td.axes['x'].labels):
            continue
        if not (td.axes['y'].linkid or td.axes['y'].addr or td.axes['y'].labels):
            continue
        fam.append((int(m.group(1)), td))
    fam.sort(key=lambda e: e[0])
    return [td for _, td in fam]


def resolve_axis(adef, tables):
    """Resolve a shared axis to its authoritative storage (linked table's z axis)."""
    if adef.linkid and adef.linkid in tables:
        src = tables[adef.linkid]
        a = AxisDef()
        a.addr = src.addr
        a.n = max(src.rows, src.cols)
        a.scale = src.scale
        return a
    return adef


# -----------------------------------------------------------------------------
# Log handling
# -----------------------------------------------------------------------------
def _norm(s):
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', s.lower()).split())


CHANNEL_PATTERNS = {
    'rpm':       ['engine speed rpm', 'engine rpm', 'rpm', 'engine speed'],
    'map':       ['map bar', 'map kpa', 'map', 'manifold air pressure', 'manifold pressure'],
    'vetable':   ['active ve table u', 'active ve table', 've table', 'active ve'],
    'afr1':      ['afr 1 afr', 'afr1 afr', 'afr 1', 'afr1', 'wideband 1 afr', 'lambda 1'],
    'afr2':      ['afr 2 afr', 'afr2 afr', 'afr 2', 'afr2', 'wideband 2 afr', 'lambda 2'],
    'afrtarget': ['afr target', 'target afr', 'afr targ', 'lambda target'],
    'stft1':     ['stft 1', 'stft1', 'short term fuel trim bank 1'],
    'stft2':     ['stft 2', 'stft2'],
    'ltft1':     ['ltft 1', 'ltft1', 'long term fuel trim bank 1'],
    'ltft2':     ['ltft 2', 'ltft2'],
    'inj':       ['fuel inj ms', 'fuel inj', 'injection time ms', 'inj time'],
    'clt':       ['coolant temp c', 'coolant temp', 'coolant temperature'],
    'time':      ['timestamp ms', 'timestamp', 'time s', 'time'],
}
CHANNEL_LABELS = {
    'rpm': 'Engine speed (RPM) *', 'map': 'Manifold pressure *',
    'vetable': 'Active VE table *', 'afr1': 'Wideband AFR bank 1',
    'afr2': 'Wideband AFR bank 2', 'afrtarget': 'AFR target *',
    'stft1': 'STFT bank 1 (%)', 'stft2': 'STFT bank 2 (%)',
    'ltft1': 'LTFT bank 1 (%)', 'ltft2': 'LTFT bank 2 (%)',
    'inj': 'Injection time (ms)', 'clt': 'Coolant temp (°C)', 'time': 'Timestamp',
}
REQUIRED = ('rpm', 'map', 'vetable', 'afrtarget')


def guess_channels(header):
    normed = [_norm(h) for h in header]
    found = {}
    for key, pats in CHANNEL_PATTERNS.items():
        for p in pats:
            hit = next((i for i, nh in enumerate(normed) if nh == p), None)
            if hit is None:
                hit = next((i for i, nh in enumerate(normed) if p in nh), None)
            if hit is not None and hit not in found.values():
                found[key] = hit
                break
    # keep ltft multiplicative, drop 'add' variants
    for k in ('ltft1', 'ltft2'):
        if k in found and 'add' in normed[found[k]]:
            alt = next((i for i, nh in enumerate(normed)
                        if _norm(CHANNEL_PATTERNS[k][0]) in nh and 'add' not in nh), None)
            if alt is not None and alt not in found.values():
                found[k] = alt
            else:
                found.pop(k)
    return found


def guess_map_scale(header, col):
    if col is None:
        return 100.0
    u = _norm(header[col])
    if 'kpa' in u:
        return 1.0
    if 'bar' in u:
        return 100.0
    return 100.0


class LogFile:
    """One parsed CSV log: numeric columns cached as float arrays."""
    def __init__(self, path):
        self.path = path
        self.header = []
        self.rows = 0
        self.data = None          # dict col_idx -> np.ndarray
        self.error = None
        self._parse()

    def _parse(self):
        try:
            data = _read_bytes_robust(self.path)
            text = data.decode('utf-8-sig', errors='replace')
            rdr = csv.reader(io.StringIO(text, newline=''))
            self.header = next(rdr, [])
            ncols = len(self.header)
            cols = [[] for _ in range(ncols)]
            n = 0
            for row in rdr:
                if not row or all(not c.strip() for c in row):
                    continue
                for i in range(ncols):
                    v = row[i] if i < len(row) else ''
                    try:
                        cols[i].append(float(v) if v.strip() else np.nan)
                    except ValueError:
                        cols[i].append(np.nan)
                n += 1
            self.rows = n
            self.data = {i: np.asarray(c, dtype=np.float64) for i, c in enumerate(cols)}
        except Exception as e:  # noqa: BLE001
            self.error = str(e)

    def col(self, idx):
        return self.data.get(idx)


# ----------------------------------------------------------------------------- 
# Tuner core (pure logic, no Qt)
# ----------------------------------------------------------------------------- 
class Tuner:
    """Accumulates log evidence against a family of VE tables and computes new values."""

    def __init__(self):
        self.tables = []          # list[TableDef]
        self.rpm_ax = None        # np.ndarray breakpoints
        self.map_ax = None
        self.big = False
        self.orig = []            # list[np.ndarray] scaled values
        self.orig_raw = []        # list[np.ndarray] int
        self.W = None             # (nt, r, c) effective sample weight
        self.C = None             # weighted correction factor sums
        self.E = None             # weighted afr-error sums (no trim)
        self.new = None           # list[np.ndarray] scaled new values
        self.new_raw = []
        self.stats = {}
        self.table_stats = []

    # ---- binary ----
    def read_tables(self, bindata, tables, xaxis, yaxis):
        self.tables = tables
        nt = len(tables)
        endians = []
        for cand in (False, True):
            dt = np.dtype(('>' if cand else '<') + 'u2')
            vals = np.frombuffer(bindata[xaxis.addr:xaxis.addr + 2 * xaxis.n], dtype=dt).astype(np.float64)
            if (len(vals) == xaxis.n and np.all(np.diff(vals) >= 0)
                    and vals.min() >= 0 and vals.max() <= 10000.5):
                endians.append(cand)
        self.big = endians[0] if endians else False

        dt = np.dtype(('>' if self.big else '<') + 'u2')
        self.rpm_ax = np.frombuffer(bindata[xaxis.addr:xaxis.addr + 2 * xaxis.n],
                                    dtype=dt).astype(np.float64) * xaxis.scale
        self.map_ax = np.frombuffer(bindata[yaxis.addr:yaxis.addr + 2 * yaxis.n],
                                    dtype=dt).astype(np.float64) * yaxis.scale
        self.orig_raw = []
        self.orig = []
        for td in tables:
            raw = np.frombuffer(bindata[td.addr:td.addr + td.nbytes], dtype=dt).astype(np.int64)
            self.orig_raw.append(raw.reshape(td.rows, td.cols).copy())
            self.orig.append(raw.reshape(td.rows, td.cols).astype(np.float64) * td.scale)
        self.nt = nt

    def raw_bounds(self, td):
        lo = 0 if td.vmin is None else int(np.ceil(td.vmin / td.scale))
        hi = 65535 if td.vmax is None else int(np.floor(td.vmax / td.scale))
        return max(0, lo), min(65535, hi)

    # ---- analysis ----
    def analyze(self, samples, params):
        """samples: dict of np arrays (rpm, mapkpa, avt, afr, target, trim).
        params: dict of filter options.  Fills W/C/E and stats."""
        nt = self.nt
        r = self.rpm_ax
        m = self.map_ax
        self.W = np.zeros((nt, len(m), len(r)))
        self.C = np.zeros_like(self.W)
        self.E = np.zeros_like(self.W)

        rpm = samples['rpm']; mp = samples['mapkpa']; avt = samples['avt']
        afr = samples['afr']; tgt = samples['target']
        trim = samples.get('trim')

        reasons = {}
        mask = np.isfinite(rpm) & np.isfinite(mp) & np.isfinite(avt) & np.isfinite(afr) & np.isfinite(tgt)
        reasons['missing values'] = int((~mask).sum())
        with np.errstate(invalid='ignore'):
            m2 = mask & (rpm >= params['rpm_min']) & (rpm <= params['rpm_max'])
        reasons['RPM out of range'] = int((mask & ~m2).sum()); mask = m2
        m2 = mask & (afr >= params['afr_min']) & (afr <= params['afr_max'])
        reasons['AFR implausible'] = int((mask & ~m2).sum()); mask = m2
        m2 = mask & (tgt >= 9.0) & (tgt <= 18.0)
        reasons['target implausible'] = int((mask & ~m2).sum()); mask = m2
        if params.get('use_inj') and samples.get('inj') is not None:
            inj = samples['inj']
            m2 = mask & np.where(np.isfinite(inj), inj > params['inj_min'], True)
            reasons['decel / fuel cut'] = int((mask & ~m2).sum()); mask = m2
        if params.get('use_clt') and samples.get('clt') is not None:
            clt = samples['clt']
            m2 = mask & np.where(np.isfinite(clt), clt >= params['clt_min'], True)
            reasons['engine cold'] = int((mask & ~m2).sum()); mask = m2
        if params.get('use_transient'):
            ts = samples.get('time')
            dt = np.diff(ts, prepend=ts[0]) if ts is not None else np.full(len(rpm), 0.02)
            dt = np.where(dt > 1e-4, dt, np.nan)
            dr = np.abs(np.diff(rpm, prepend=rpm[0])) / dt
            dm = np.abs(np.diff(mp, prepend=mp[0])) / dt
            ok = ~ (np.nan_to_num(dr, nan=0) > params['trans_rpm'] * 1000)
            ok &= ~ (np.nan_to_num(dm, nan=0) > params['trans_map'])
            m2 = mask & ok
            reasons['transient (rpm/map slew)'] = int((mask & ~m2).sum()); mask = m2

        self.stats = {
            'total': int(len(rpm)),
            'used': int(mask.sum()),
            'reasons': reasons,
        }
        if not mask.any():
            return

        tv0 = avt[mask]
        if np.nanmax(tv0) < 1.0:          # 0-based table index logging
            tv0 = tv0 + 1.0
        tvc = np.clip(tv0, 1.0, float(nt))     # continuous 1..nt (VANOS blend)
        tlo = np.clip(np.floor(tvc).astype(int) - 1, 0, nt - 1)   # 0-based lower table
        frac = tvc - np.floor(tvc)          # blend weight of the upper table
        thi = np.clip(tlo + 1, 0, nt - 1)
        if not params.get('blend_tables', True):
            # attribute each sample wholly to the nearest table
            tlo = np.clip(np.rint(tvc).astype(int) - 1, 0, nt - 1)
            thi, frac = tlo, np.zeros_like(frac)

        x = np.clip(rpm[mask], r[0], r[-1])
        y = np.clip(mp[mask], m[0], m[-1])
        corr = afr[mask] / tgt[mask]
        err = corr - 1.0
        if trim is not None:
            corr = corr * (1.0 + trim[mask] / 100.0)

        jx = np.clip(np.searchsorted(r, x, side='right') - 1, 0, len(r) - 2)
        fx = (x - r[jx]) / (r[np.minimum(jx + 1, len(r) - 1)] - r[jx])
        fx = np.clip(np.nan_to_num(fx), 0, 1)
        jy = np.clip(np.searchsorted(m, y, side='right') - 1, 0, len(m) - 2)
        fy = (y - m[jy]) / (m[np.minimum(jy + 1, len(m) - 1)] - m[jy])
        fy = np.clip(np.nan_to_num(fy), 0, 1)

        # table blend (floor/ceil) x bilinear MAP/RPM: up to 8 cells per sample
        for tt, tw in ((tlo, 1.0 - frac), (thi, frac)):
            if not np.any(tw > 0):
                continue
            for jr, wy in ((jy, 1 - fy), (jy + 1, fy)):
                for jc, wx in ((jx, 1 - fx), (jx + 1, fx)):
                    w = tw * wy * wx
                    np.add.at(self.W, (tt, jr, jc), w)
                    np.add.at(self.C, (tt, jr, jc), w * corr)
                    np.add.at(self.E, (tt, jr, jc), w * err)

    def factors(self, smoothing=0):
        """Weighted-mean correction factor field per table (1.0 = no data)."""
        out = []
        for t in range(self.nt):
            hit = self.W[t] > 0
            f = np.where(hit, self.C[t] / np.maximum(self.W[t], 1e-9), 1.0)
            if smoothing > 0 and hit.any():
                # 4-neighbour weighted average with edge padding (no wrap-around)
                fp = np.pad(f, 1, mode='edge')
                hp = np.pad(hit.astype(float), 1, mode='constant')
                acc = f * hit * 2.0
                wacc = hit.astype(float) * 2.0
                for sf, sh in ((fp[2:, 1:-1], hp[2:, 1:-1]),
                               (fp[:-2, 1:-1], hp[:-2, 1:-1]),
                               (fp[1:-1, 2:], hp[1:-1, 2:]),
                               (fp[1:-1, :-2], hp[1:-1, :-2])):
                    acc = acc + sf * sh
                    wacc = wacc + sh
                smooth = np.where(wacc > 0, acc / np.maximum(wacc, 1e-9), f)
                a = smoothing / 100.0
                f = np.where(hit, (1 - a) * f + a * smooth, f)
            out.append(f)
        return out

    def apply(self, params):
        """Compute new table values from W/C and tuning params."""
        strength = params['strength'] / 100.0
        maxd = params['max_change'] / 100.0
        mins = params['min_samples']
        factors = self.factors(params.get('smoothing', 0))
        self.new = []
        self.new_raw = []
        self.table_stats = []
        for t, td in enumerate(self.tables):
            old = self.orig[t]
            raw = self.orig_raw[t]
            hit = self.W[t] >= mins
            f = factors[t]
            adj = np.clip(1.0 + strength * (f - 1.0), 1.0 - maxd, 1.0 + maxd)
            lo, hi = self.raw_bounds(td)
            # clamp the rounded raw result too, so the written delta never
            # exceeds max_change even after 1/1024 quantisation
            rlo = np.ceil(raw * (1.0 - maxd) - 1e-9).astype(np.int64)
            rhi = np.floor(raw * (1.0 + maxd) + 1e-9).astype(np.int64)
            rlo = np.maximum(rlo, lo)
            rhi = np.minimum(np.maximum(rhi, rlo), hi)
            newraw = np.where(hit, np.clip(np.rint(raw * adj), rlo, rhi), raw).astype(np.int64)
            self.new_raw.append(newraw)
            self.new.append(newraw * td.scale)
            d = np.where(hit & (raw > 0), (newraw / np.maximum(raw, 1)) - 1.0, 0.0)
            hh = self.W[t] > 0
            self.table_stats.append({
                'hit': int(hh.sum()),
                'tuned': int(hit.sum()),
                'samples': float(self.W[t][hh].sum()) if hh.any() else 0.0,
                'fac_mean': float(f[hh].mean()) if hh.any() else 1.0,
                'dmin': float(d.min()) if hit.any() else 0.0,
                'dmax': float(d.max()) if hit.any() else 0.0,
            })

    def write_binary(self, orig_bytes):
        out = bytearray(orig_bytes)
        for t, td in enumerate(self.tables):
            blob = self.new_raw[t].astype(f"{'>' if self.big else '<'}u2").tobytes()
            out[td.addr:td.addr + td.nbytes] = blob
        return bytes(out)


# ----------------------------------------------------------------------------- 
# GUI helpers
# ----------------------------------------------------------------------------- 
def lerp_color(c1, c2, t):
    return QColor(int(c1.red() + (c2.red() - c1.red()) * t),
                  int(c1.green() + (c2.green() - c1.green()) * t),
                  int(c1.blue() + (c2.blue() - c1.blue()) * t))


def heat_bg(delta, vmax):
    """delta: fractional change; vmax: full-scale |delta| for color."""
    if delta is None or not np.isfinite(delta) or abs(delta) < 1e-9:
        return HEAT_NONE
    t = min(1.0, abs(delta) / max(vmax, 1e-9))
    base = HEAT_NONE
    tgt = HEAT_LEAN if delta > 0 else HEAT_RICH
    return lerp_color(base, tgt, 0.38 + 0.62 * t)


class VeTableView(QTableWidget):
    """16x16 VE grid with row/col breakpoints and editable new values."""

    MODE_TUNED, MODE_ORIG, MODE_DELTA, MODE_FACTOR, MODE_SAMPLES = range(5)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFont(QFont(MONO, 9))
        self.mode = self.MODE_TUNED
        self.data = None      # dict with arrays
        self.verticalHeader().setDefaultSectionSize(26)
        self.horizontalHeader().setDefaultSectionSize(58)
        self.verticalHeader().setVisible(True)
        self.itemChanged.connect(self._on_item_changed)

    # -- populate --
    def set_data(self, d):
        self.data = d
        self._block = True
        self.setRowCount(len(d['map_ax']))
        self.setColumnCount(len(d['rpm_ax']))
        self.setHorizontalHeaderLabels([f"{int(v)}" for v in d['rpm_ax']])
        self.setVerticalHeaderLabels([f"{v:.0f}" for v in d['map_ax']])
        for rr in range(len(d['map_ax'])):
            for cc in range(len(d['rpm_ax'])):
                it = QTableWidgetItem()
                it.setTextAlignment(Qt.AlignCenter)
                self.setItem(rr, cc, it)
        # columns stretch to fill available width; rows keep fixed height
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self._block = False
        self.refresh()

    def refresh(self):
        if self.data is None:
            return
        d = self.data
        self._block = True
        editing = (self.mode == self.MODE_TUNED)
        for rr in range(len(d['map_ax'])):
            for cc in range(len(d['rpm_ax'])):
                it = self.item(rr, cc)
                hit = d['W'][rr, cc] > 0
                tuned = d['hit'][rr, cc]
                delta = (d['new'][rr, cc] / d['orig'][rr, cc] - 1.0) if d['orig'][rr, cc] > 0 else 0.0
                fac = d['fac'][rr, cc]
                if self.mode == self.MODE_TUNED:
                    txt = f"{d['new'][rr, cc]:.3f}"
                elif self.mode == self.MODE_ORIG:
                    txt = f"{d['orig'][rr, cc]:.3f}"
                elif self.mode == self.MODE_DELTA:
                    txt = f"{delta * 100:+.1f}" if tuned else ("·" if not hit else "0.0")
                elif self.mode == self.MODE_FACTOR:
                    txt = f"{fac:.3f}" if hit else "·"
                else:
                    txt = f"{d['W'][rr, cc]:.0f}" if hit else "·"
                it.setText(txt)
                vmax = max(d['vmax_display'], 1e-9)
                if self.mode == self.MODE_SAMPLES:
                    if hit:
                        t = min(1.0, d['W'][rr, cc] / max(d['wmax'], 1e-9))
                        it.setBackground(lerp_color(HEAT_NONE, HEAT_COUNT, 0.25 + 0.75 * t))
                        it.setForeground(QBrush(QColor('#eaf5ec')))
                    else:
                        it.setBackground(HEAT_NONE)
                        it.setForeground(QBrush(QColor(C_TEXT_DIM)))
                else:
                    bg = heat_bg(delta if tuned else 0.0, vmax)
                    it.setBackground(bg)
                    it.setForeground(QBrush(QColor('#ffffff') if tuned or hit else QColor(C_TEXT_DIM)))
                f = Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsEditable
                it.setFlags(f if editing and tuned else f & ~Qt.ItemIsEditable)
                it.setToolTip(
                    f"MAP {d['map_ax'][rr]:.0f} kPa @ {int(d['rpm_ax'][cc])} rpm\n"
                    f"original: {d['orig'][rr, cc]:.3f}\n"
                    f"new:      {d['new'][rr, cc]:.3f}  ({delta * 100:+.1f}%)\n"
                    f"factor:   {fac:.3f}\n"
                    f"samples:  {d['W'][rr, cc]:.1f}")
        self._block = False

    def _on_item_changed(self, item):
        if getattr(self, '_block', False) or self.data is None:
            return
        if self.mode != self.MODE_TUNED:
            return
        try:
            v = float(item.text().replace(',', '.'))
        except ValueError:
            self.refresh()
            return
        d = self.data
        rr, cc = item.row(), item.column()
        raw = int(np.clip(np.rint(v / d['scale']), d['raw_lo'], d['raw_hi']))
        d['new_raw'][rr, cc] = raw
        d['new'][rr, cc] = raw * d['scale']
        d['edited'] = True
        self.refresh()


class ElidedLabel(QLabel):
    """QLabel that elides text in the middle instead of hard-clipping."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._full = ""

    def setFullText(self, text):
        self._full = text
        self._apply()

    def _apply(self):
        fm = self.fontMetrics()
        self.setText(fm.elidedText(self._full, Qt.ElideMiddle, max(10, self.width())))

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._apply()


class LegendBar(QWidget):
    """Small gradient legend; adapts to the current table view mode."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(22)
        self.setMinimumWidth(250)
        self.mode = VeTableView.MODE_TUNED
        self.lbl_left = "-15%"
        self.lbl_right = "+15%"

    def set_mode(self, mode, vmax=0.15, wmax=1.0):
        self.mode = mode
        if mode == VeTableView.MODE_SAMPLES:
            self.lbl_left = "0"
            self.lbl_right = f"{wmax:.0f}"
        else:
            self.lbl_left = f"-{vmax * 100:.0f}%"
            self.lbl_right = f"+{vmax * 100:.0f}%"
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        w, h = self.width(), self.height()
        rm = 46                      # right margin for the max label
        gw = w - rm
        grad = QLinearGradient(0, 0, gw, 0)
        if self.mode == VeTableView.MODE_SAMPLES:
            for t in np.linspace(0, 1, 11):
                grad.setColorAt(t, lerp_color(HEAT_NONE, HEAT_COUNT, 0.38 + 0.62 * t))
        else:
            for t in np.linspace(0, 1, 11):
                grad.setColorAt(t, heat_bg(-1 + 2 * t, 1.0))
        p.fillRect(0, 4, gw, h - 8, grad)
        p.setPen(QPen(QColor('#000000'), 1))
        p.drawRect(0, 4, gw - 1, h - 9)
        f = QFont('Segoe UI', 7)
        p.setFont(f)
        p.setPen(QColor(C_TEXT_DIM))
        p.drawText(QRect(gw + 2, 0, rm - 2, h), Qt.AlignVCenter | Qt.AlignLeft, self.lbl_right)
        p.setPen(QColor('#000000'))
        p.drawText(3, 15, self.lbl_left)
        p.setPen(QColor('#ffffff'))
        p.drawText(2, 14, self.lbl_left)
        p.end()


# ----------------------------------------------------------------------------- 
# Main window
# ----------------------------------------------------------------------------- 
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.setWindowIcon(self._make_icon())
        self.resize(1380, 860)
        self.setMinimumSize(1100, 700)

        self.settings = QSettings("MS4X Tools", "VE Auto-Tune")

        # state
        self.xdf_path = None
        self.xdf_base = 0
        self.xdf_tables = {}
        self.ve_tables = []
        self.bin_path = None
        self.bin_data = None
        self.logs = []            # list[LogFile]
        self.tuner = Tuner()
        self.analysis_valid = False
        self.apply_valid = False

        self._build_ui()
        self._load_settings()
        self._refresh_recent_combos()
        self._refresh_state()

    # ------------------------------------------------------------------ settings / recents
    def _load_settings(self):
        s = self.settings
        self._dirs = {
            'xdf': s.value("dir_xdf", "", str),
            'bin': s.value("dir_bin", "", str),
            'log': s.value("dir_log", "", str),
        }
        legacy = s.value("last_dir", "", str)      # migrate pre-1.1 setting
        for k, v in self._dirs.items():
            if not v and legacy and os.path.isdir(legacy):
                self._dirs[k] = legacy
        self.recent = {}
        for k in ('xdf', 'bin', 'log'):
            raw = s.value(f"recent_{k}", "", str)
            try:
                lst = json.loads(raw) if raw else []
            except ValueError:
                lst = []
            self.recent[k] = [p for p in lst if isinstance(p, str)][:10]

    def _save_recents(self):
        for k in ('xdf', 'bin', 'log'):
            self.settings.setValue(f"recent_{k}", json.dumps(self.recent.get(k, [])))

    def _remember_dir(self, kind, path):
        d = os.path.dirname(path)
        if d:
            self._dirs[kind] = d
            self.settings.setValue(f"dir_{kind}", d)

    def _push_recent(self, kind, path):
        path = os.path.abspath(path)
        lst = self.recent.setdefault(kind, [])
        if path in lst:
            lst.remove(path)
        lst.insert(0, path)
        del lst[10:]
        self._save_recents()
        self._refresh_recent_combos()

    def _refresh_recent_combos(self):
        for kind, cmb in getattr(self, 'recent_combos', {}).items():
            cmb.blockSignals(True)
            cmb.clear()
            cmb.addItem("— recent —", None)
            for p in self.recent.get(kind, []):
                cmb.addItem(os.path.basename(p), p)
                cmb.setItemData(cmb.count() - 1, p, Qt.ToolTipRole)
            cmb.blockSignals(False)
            cmb.setCurrentIndex(0)

    def _recent_selected(self, kind, idx):
        cmb = self.recent_combos[kind]
        path = cmb.itemData(idx)
        if not path:
            return
        if not os.path.isfile(path):
            QMessageBox.warning(self, "File not found",
                                f"{path}\nno longer exists — removed from the recent list.")
            try:
                self.recent[kind].remove(path)
            except ValueError:
                pass
            self._save_recents()
            self._refresh_recent_combos()
            return
        if kind == 'xdf':
            self.load_xdf(path)
        elif kind == 'bin':
            self.load_bin(path)
        else:
            self._add_log_paths([path])
        cmb.blockSignals(True)
        cmb.setCurrentIndex(0)
        cmb.blockSignals(False)

    # ------------------------------------------------------------------ UI
    @staticmethod
    def _make_icon():
        pm = QPixmap(256, 256)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(8, 8, 240, 240, 48, 48)
        p.fillPath(path, QColor('#21252b'))
        p.setPen(QPen(QColor('#2f81f7'), 8))
        p.drawRoundedRect(8, 8, 240, 240, 48, 48)
        # gauge arc
        p.setPen(QPen(QColor('#3fb950'), 10, Qt.SolidLine, Qt.RoundCap))
        rect = QRect(48, 40, 160, 160)
        p.drawArc(rect, 200 * 16, -140 * 16)
        p.setPen(QPen(QColor('#f85149'), 10, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(rect, 60 * 16, -40 * 16)
        # needle
        p.setPen(QPen(QColor('#d7dde4'), 6, Qt.SolidLine, Qt.RoundCap))
        import math
        ang = math.radians(-20)
        cx, cy = 128, 120
        p.drawLine(int(cx), int(cy), int(cx + 70 * math.cos(ang)), int(cy - 70 * math.sin(ang)))
        p.setPen(QPen(QColor('#d7dde4'), 4))
        p.drawEllipse(int(cx - 8), int(cy - 8), 16, 16)
        f = QFont('Segoe UI', 64, QFont.Bold)
        p.setFont(f)
        p.setPen(QColor('#ffffff'))
        p.drawText(QRect(0, 130, 256, 90), Qt.AlignHCenter, "VE")
        p.end()
        return QIcon(pm)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter)

        # ============ LEFT ============
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(8)

        # -- 1 definitions
        g1 = QGroupBox("1 · Definition & binary")
        f1 = QGridLayout(g1)
        self.btn_xdf = QPushButton("Load XDF…")
        self.btn_bin = QPushButton("Load binary…")
        self.btn_bin.setEnabled(False)
        self.lbl_xdf = QLabel("—")
        self.lbl_bin = QLabel("—")
        self.lbl_xdf.setStyleSheet(f"color: {C_TEXT_DIM};")
        self.lbl_bin.setStyleSheet(f"color: {C_TEXT_DIM};")
        f1.addWidget(self.btn_xdf, 0, 0)
        f1.addWidget(self.lbl_xdf, 0, 1)
        f1.addWidget(self.btn_bin, 1, 0)
        f1.addWidget(self.lbl_bin, 1, 1)
        f1.setColumnStretch(1, 1)
        self.lbl_definfo = QLabel("")
        self.lbl_definfo.setStyleSheet(f"color: {C_TEXT_DIM}; font-size: 8.5pt;")
        f1.addWidget(self.lbl_definfo, 2, 0, 1, 2)

        # recent files dropdowns
        self.recent_combos = {}
        rx = QLabel("Recent XDF:")
        rx.setStyleSheet(f"color: {C_TEXT_DIM}; font-size: 8.5pt;")
        self.cmb_recent_xdf = QComboBox()
        rb = QLabel("Recent binary:")
        rb.setStyleSheet(f"color: {C_TEXT_DIM}; font-size: 8.5pt;")
        self.cmb_recent_bin = QComboBox()
        for kind, cmb in (('xdf', self.cmb_recent_xdf), ('bin', self.cmb_recent_bin)):
            cmb.setToolTip("Recently used files (full path shown on hover)")
            cmb.currentIndexChanged.connect(
                lambda i, k=kind: self._recent_selected(k, i))
            self.recent_combos[kind] = cmb
        f1.addWidget(rx, 3, 0)
        f1.addWidget(self.cmb_recent_xdf, 3, 1)
        f1.addWidget(rb, 4, 0)
        f1.addWidget(self.cmb_recent_bin, 4, 1)
        lv.addWidget(g1)

        # -- 2 logs
        g2 = QGroupBox("2 · Datalogs")
        v2 = QVBoxLayout(g2)
        row = QHBoxLayout()
        self.btn_addlog = QPushButton("Add CSV logs…")
        self.btn_rmlog = QPushButton("Remove")
        self.btn_rmlog.setEnabled(False)
        row.addWidget(self.btn_addlog)
        row.addWidget(self.btn_rmlog)
        row.addStretch(1)
        v2.addLayout(row)
        self.list_logs = QListWidget()
        self.list_logs.setMaximumHeight(96)
        v2.addWidget(self.list_logs)
        rl = QLabel("Recent log:")
        rl.setStyleSheet(f"color: {C_TEXT_DIM}; font-size: 8.5pt;")
        self.cmb_recent_log = QComboBox()
        self.cmb_recent_log.setToolTip("Recently used log files (full path shown on hover)")
        self.cmb_recent_log.currentIndexChanged.connect(lambda i: self._recent_selected('log', i))
        self.recent_combos['log'] = self.cmb_recent_log
        h = QHBoxLayout()
        h.addWidget(rl)
        h.addWidget(self.cmb_recent_log, 1)
        v2.addLayout(h)

        chbox = QHBoxLayout()
        chl = QLabel("MAP logged in:")
        chl.setStyleSheet(f"color:{C_TEXT_DIM}")
        self.cmb_mapscale = QComboBox()
        self.cmb_mapscale.addItems(["Bar (auto)", "kPa (auto)", "Bar", "kPa"])
        chbox.addWidget(chl)
        chbox.addWidget(self.cmb_mapscale)
        chbox.addStretch(1)
        v2.addLayout(chbox)

        self.grid_channels = QGridLayout()
        self.grid_channels.setVerticalSpacing(3)
        v2.addLayout(self.grid_channels)
        self.channel_combos = {}
        self._build_channel_grid()

        afrrow = QHBoxLayout()
        al = QLabel("AFR source:")
        al.setStyleSheet(f"color:{C_TEXT_DIM}")
        self.cmb_afrsrc = QComboBox()
        self.cmb_afrsrc.addItems(["Average of banks", "Bank 1 only", "Bank 2 only"])
        afrrow.addWidget(al)
        afrrow.addWidget(self.cmb_afrsrc)
        afrrow.addStretch(1)
        v2.addLayout(afrrow)
        self.chk_trim = QCheckBox("Include STFT/LTFT in correction (closed loop)")
        self.chk_trim.setChecked(False)
        v2.addWidget(self.chk_trim)
        lv.addWidget(g2)

        # -- 3 filters
        g3 = QGroupBox("3 · Sample filters")
        f3 = QFormLayout(g3)
        f3.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.sp_rpm_min = QSpinBox(); self.sp_rpm_min.setRange(0, 12000); self.sp_rpm_min.setValue(300)
        self.sp_rpm_max = QSpinBox(); self.sp_rpm_max.setRange(0, 12000); self.sp_rpm_max.setValue(7500)
        h = QHBoxLayout(); h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.sp_rpm_min); h.addWidget(QLabel("…")); h.addWidget(self.sp_rpm_max)
        f3.addRow("Engine speed (rpm)", h)
        self.sp_afr_min = QDoubleSpinBox(); self.sp_afr_min.setRange(8, 20); self.sp_afr_min.setValue(10.0)
        self.sp_afr_max = QDoubleSpinBox(); self.sp_afr_max.setRange(8, 22); self.sp_afr_max.setValue(19.0)
        h = QHBoxLayout(); h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.sp_afr_min); h.addWidget(QLabel("…")); h.addWidget(self.sp_afr_max)
        f3.addRow("Plausible AFR", h)

        self.chk_inj = QCheckBox("Exclude fuel cut, inj <")
        self.chk_inj.setChecked(True)
        self.chk_inj.setToolTip("Skip samples where injection time is below the threshold\n"
                                "(deceleration fuel cut produces meaningless AFR).")
        self.sp_inj = QDoubleSpinBox(); self.sp_inj.setRange(0, 10); self.sp_inj.setSingleStep(0.1); self.sp_inj.setValue(0.5)
        self.sp_inj.setSuffix(" ms")
        h = QHBoxLayout(); h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.chk_inj); h.addWidget(self.sp_inj)
        f3.addRow("", h)

        self.chk_clt = QCheckBox("Warm engine, coolant ≥")
        self.chk_clt.setChecked(True)
        self.chk_clt.setToolTip("Skip samples from a cold engine (enrichment skews AFR error).")
        self.sp_clt = QDoubleSpinBox(); self.sp_clt.setRange(-30, 130); self.sp_clt.setValue(60.0)
        self.sp_clt.setSuffix(" °C")
        h = QHBoxLayout(); h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.chk_clt); h.addWidget(self.sp_clt)
        f3.addRow("", h)

        self.chk_trans = QCheckBox("Skip transients")
        self.chk_trans.setToolTip("Skip samples where engine speed or manifold pressure changes\n"
                                  "too fast for the VE model (tip-in / lift-off).")
        self.sp_trans_rpm = QDoubleSpinBox(); self.sp_trans_rpm.setRange(0.1, 20); self.sp_trans_rpm.setSingleStep(0.5); self.sp_trans_rpm.setValue(2.0)
        self.sp_trans_rpm.setSuffix(" k rpm/s")
        self.sp_trans_map = QDoubleSpinBox(); self.sp_trans_map.setRange(1, 2000); self.sp_trans_map.setSingleStep(10); self.sp_trans_map.setValue(200.0)
        self.sp_trans_map.setSuffix(" kPa/s")
        h = QHBoxLayout(); h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.chk_trans); h.addWidget(self.sp_trans_rpm); h.addWidget(self.sp_trans_map)
        f3.addRow("", h)
        lv.addWidget(g3)

        # -- 4 tuning
        g4 = QGroupBox("4 · Tuning")
        f4 = QFormLayout(g4)
        self.sl_strength = QSlider(Qt.Horizontal)
        self.sl_strength.setRange(5, 100)
        self.sl_strength.setValue(100)
        self.lbl_strength = QLabel("100 %")
        h = QHBoxLayout(); h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.sl_strength); h.addWidget(self.lbl_strength)
        f4.addRow("Correction strength", h)
        self.sp_maxchg = QDoubleSpinBox(); self.sp_maxchg.setRange(1, 50); self.sp_maxchg.setSingleStep(0.5); self.sp_maxchg.setValue(15.0)
        self.sp_maxchg.setSuffix(" %")
        f4.addRow("Max change per cell", self.sp_maxchg)
        self.sp_minsamp = QDoubleSpinBox(); self.sp_minsamp.setRange(1, 500); self.sp_minsamp.setSingleStep(1); self.sp_minsamp.setValue(5.0)
        f4.addRow("Min effective samples", self.sp_minsamp)
        self.sp_smooth = QDoubleSpinBox(); self.sp_smooth.setRange(0, 100); self.sp_smooth.setSingleStep(5); self.sp_smooth.setValue(0.0)
        self.sp_smooth.setSuffix(" %")
        f4.addRow("Neighbor smoothing", self.sp_smooth)
        self.chk_blend = QCheckBox("Blend samples across adjacent VE tables")
        self.chk_blend.setChecked(True)
        self.chk_blend.setToolTip(
            "The ECU interpolates VE between valve-overlap steps as VANOS moves,\n"
            "so 'active ve table' is fractional (e.g. 4.3 = 70% table 4 + 30% table 5).\n"
            "When enabled, each sample's correction is split between the two adjacent\n"
            "tables with those weights — matching how the ECU actually blends fueling.\n"
            "Uncheck to attribute each sample wholly to the nearest table.")
        f4.addRow("Attribution", self.chk_blend)
        lv.addWidget(g4)

        # -- actions
        self.btn_analyze = QPushButton("Analyze logs")
        self.btn_analyze.setProperty("accent", True)
        self.btn_analyze.setEnabled(False)
        lv.addWidget(self.btn_analyze)
        self.btn_revert = QPushButton("Revert all tables to original")
        self.btn_revert.setEnabled(False)
        lv.addWidget(self.btn_revert)
        self.btn_write = QPushButton("Write tuned binary…")
        self.btn_write.setProperty("accent", True)
        self.btn_write.setEnabled(False)
        self.btn_report = QPushButton("Export CSV report…")
        self.btn_report.setEnabled(False)
        h = QHBoxLayout()
        h.addWidget(self.btn_write, 1)
        h.addWidget(self.btn_report, 1)
        lv.addLayout(h)

        # -- message pane
        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setMinimumHeight(110)
        self.txt_log.setMaximumHeight(180)
        lv.addWidget(self.txt_log)

        left.setMinimumWidth(450)
        left.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Expanding)

        leftscroll = QScrollArea()
        leftscroll.setWidget(left)
        leftscroll.setWidgetResizable(True)
        leftscroll.setFrameShape(QFrame.NoFrame)
        leftscroll.setMinimumWidth(470)

        # ============ RIGHT ============
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        rv.addWidget(self.tabs)
        self.tabs.addTab(self._build_overview(), "Overview")

        splitter.addWidget(leftscroll)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([460, 900])

        # status bar
        sb = self.statusBar()
        self.lbl_status = QLabel("Load an XDF definition to begin")
        sb.addWidget(self.lbl_status, 1)

        # ---- wiring ----
        self.btn_xdf.clicked.connect(self.load_xdf)
        self.btn_bin.clicked.connect(self.load_bin)
        self.btn_addlog.clicked.connect(self.add_logs)
        self.btn_rmlog.clicked.connect(self.remove_log)
        self.btn_analyze.clicked.connect(self.analyze)
        self.btn_revert.clicked.connect(self.revert_all)
        self.btn_write.clicked.connect(self.write_binary)
        self.btn_report.clicked.connect(self.export_report)
        self.sl_strength.valueChanged.connect(
            lambda v: self.lbl_strength.setText(f"{v} %"))
        self.sl_strength.sliderReleased.connect(self._params_changed)
        self.sp_maxchg.valueChanged.connect(self._params_debounced)
        self.sp_minsamp.valueChanged.connect(self._params_debounced)
        self.sp_smooth.valueChanged.connect(self._params_debounced)
        for w in (self.sp_rpm_min, self.sp_rpm_max, self.sp_afr_min, self.sp_afr_max,
                  self.sp_inj, self.sp_clt, self.sp_trans_rpm, self.sp_trans_map):
            w.valueChanged.connect(self._params_debounced)
        for w in (self.chk_inj, self.chk_clt, self.chk_trans):
            w.toggled.connect(self._params_debounced)
        self.chk_blend.toggled.connect(self._params_debounced)
        self.chk_blend.toggled.connect(self._reanalyze_if_valid)
        self.chk_trim.toggled.connect(self._params_debounced)
        self.cmb_afrsrc.currentIndexChanged.connect(self._params_debounced)
        self.cmb_mapscale.currentIndexChanged.connect(self._channels_changed)

        self._param_debounce = QTimer(self)
        self._param_debounce.setSingleShot(True)
        self._param_debounce.setInterval(350)
        self._param_debounce.timeout.connect(self._reapply)

    def _build_channel_grid(self):
        keys = ['rpm', 'map', 'vetable', 'afrtarget', 'afr1', 'afr2',
                'stft1', 'stft2', 'ltft1', 'ltft2', 'inj', 'clt', 'time']
        for i, k in enumerate(keys):
            lbl = QLabel(CHANNEL_LABELS[k])
            lbl.setStyleSheet(f"color: {C_TEXT_DIM}; font-size: 8.5pt;")
            cmb = QComboBox()
            cmb.setMinimumContentsLength(12)
            cmb.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLength)
            self.grid_channels.addWidget(lbl, i, 0)
            self.grid_channels.addWidget(cmb, i, 1)
            self.channel_combos[k] = cmb
            cmb.currentIndexChanged.connect(lambda *_, kk=k: self._channels_changed())
        self.grid_channels.setColumnStretch(1, 1)
        self.grid_channels.setVerticalSpacing(2)

    def _build_overview(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(10)
        self.lbl_overview = QLabel("No analysis yet — load definition, binary and logs, then press Analyze.")
        self.lbl_overview.setWordWrap(True)
        v.addWidget(self.lbl_overview)
        self.tbl_overview = QTableWidget()
        self.tbl_overview.setColumnCount(7)
        self.tbl_overview.setHorizontalHeaderLabels(
            ["Table", "Description", "Samples", "Cells w/ data", "Cells tuned",
             "Mean factor", "Δ range"])
        self.tbl_overview.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for c in (0, 2, 3, 4, 5, 6):
            self.tbl_overview.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.tbl_overview.verticalHeader().setDefaultSectionSize(24)
        self.tbl_overview.verticalHeader().setVisible(False)
        self.tbl_overview.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_overview.setFont(QFont(MONO, 9))
        v.addWidget(self.tbl_overview, 2)
        self.lbl_overview2 = QLabel("")
        self.lbl_overview2.setWordWrap(True)
        self.lbl_overview2.setStyleSheet(f"color: {C_TEXT_DIM};")
        v.addWidget(self.lbl_overview2)

        card = QFrame()
        card.setStyleSheet(f"QFrame {{ background: {C_PANEL}; border: 1px solid {C_BORDER};"
                           f" border-radius: 6px; }}")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(12, 10, 12, 10)
        ttl = QLabel("How the tune is computed")
        ttl.setStyleSheet(f"color: {C_ACCENT}; font-weight: 600;")
        cv.addWidget(ttl)
        body = QLabel(
            "Each log sample is attributed according to the active VE table channel: with blending "
            "on, a fractional value like 4.3 splits the sample 70/30 between tables 4 and 5 "
            "(matching how the ECU interpolates VE across valve-overlap steps); within each table "
            "it is spread bilinearly over the four surrounding MAP × RPM cells.\n"
            "Per-cell correction factor = weighted mean of  AFR ÷ AFR target  "
            "(× (1 + STFT/100 + LTFT/100) when trims are included).\n"
            "New VE = original × (1 + strength × (factor − 1)), clamped to ± max change and to "
            "the table limits. Only cells with enough samples are changed; everything else in "
            "the binary stays byte-identical.\n"
            "Red cells were lean (fuel added), blue cells were rich (fuel removed). "
            "Iterate: flash, log again, re-tune — aim for |Δ| < 3%.")
        body.setWordWrap(True)
        body.setStyleSheet(f"color: {C_TEXT_DIM};")
        cv.addWidget(body)
        self.lbl_axes = QLabel("")
        self.lbl_axes.setStyleSheet(f"color: {C_TEXT}; font-family: {MONO};")
        cv.addWidget(self.lbl_axes)
        v.addWidget(card, 1)
        v.addStretch(2)
        return w

    # ------------------------------------------------------------------ IO
    def log(self, msg):
        self.txt_log.append(msg)

    def _refresh_state(self):
        have_def = self.xdf_path is not None
        have_bin = self.bin_data is not None
        have_logs = bool(self.logs)
        self.btn_bin.setEnabled(have_def)
        self.btn_analyze.setEnabled(have_def and have_bin and have_logs and self._channels_ok())
        if not self.apply_valid:
            self.btn_write.setEnabled(False)
            self.btn_report.setEnabled(False)
        self.btn_rmlog.setEnabled(len(self.list_logs.selectedItems()) > 0)
        self._update_status()

    def _update_status(self):
        bits = []
        if self.xdf_path:
            bits.append(f"XDF: {os.path.basename(self.xdf_path)}")
        if self.bin_path:
            bits.append(f"BIN: {os.path.basename(self.bin_path)}")
        if self.ve_tables:
            bits.append(f"{len(self.ve_tables)} VE tables")
        if self.bin_data is not None:
            bits.append("little-endian" if not self.tuner.big else "big-endian")
        if self.logs:
            bits.append(f"{len(self.logs)} log(s)")
        self.lbl_status.setText("  ·  ".join(bits) if bits else "Load an XDF definition to begin")

    # ---- XDF ----
    def load_xdf(self, path=None):
        if path is None:
            path, _ = QFileDialog.getOpenFileName(
                self, "Select TunerPro XDF definition",
                self._dirs.get('xdf') or "", "XDF (*.xdf);;All files (*)")
            if not path:
                return
        try:
            base, tables = parse_xdf_data(_read_bytes_robust(path))
        except OSError as e:
            QMessageBox.critical(self, "File error",
                                 f"Could not read\n{path}\n\n{e}\n\nIf the file is in a "
                                 "cloud-synced folder (Dropbox/OneDrive) or was just picked "
                                 "from the dialog, wait a moment and try again, or copy it "
                                 "to a local disk.")
            return
        except ET.ParseError as e:
            QMessageBox.critical(self, "XDF error", f"Could not parse XDF:\n{e}")
            return
        self.xdf_path = path
        self.xdf_base = base
        self.xdf_tables = tables
        self.ve_tables = find_ve_family(tables)
        self._remember_dir('xdf', path)
        self._push_recent('xdf', path)
        self.lbl_xdf.setText(os.path.basename(path))
        self.lbl_xdf.setToolTip(path)
        if not self.ve_tables:
            self.lbl_definfo.setText("No VE table family found!")
            self.lbl_definfo.setStyleSheet(f"color: {C_BAD};")
            QMessageBox.warning(self, "No VE tables",
                                "No table family matching '…ve<N>…' with 2D square layout was found in this XDF.")
        else:
            self.lbl_definfo.setText(
                f"{len(self.ve_tables)} VE tables · base offset 0x{base:X}")
            self.lbl_definfo.setStyleSheet(f"color: {C_TEXT_DIM};")
        self.log(f"XDF loaded: {os.path.basename(path)}  ({len(tables)} tables, "
                 f"base offset 0x{base:X}, {len(self.ve_tables)} VE tables found)")
        # drop stale binary
        if self.bin_path:
            self.load_bin(self.bin_path)
        self._refresh_state()

    # ---- BIN ----
    def load_bin(self, path=None):
        if path is None:
            path, _ = QFileDialog.getOpenFileName(
                self, "Select binary (.bin)",
                self._dirs.get('bin') or "", "Binary (*.bin);;All files (*)")
            if not path:
                return
        if not self.ve_tables:
            QMessageBox.warning(self, "No definition", "Load an XDF with VE tables first.")
            return
        try:
            data = _read_bytes_robust(path)
        except OSError as e:
            QMessageBox.critical(self, "File error",
                                 f"Could not read\n{path}\n\n{e}\n\nIf the file is in a "
                                 "cloud-synced folder (Dropbox/OneDrive) or was just picked "
                                 "from the dialog, wait a moment and try again, or copy it "
                                 "to a local disk.")
            return
        maxend = max(td.addr + td.nbytes for td in self.ve_tables)
        if maxend > len(data):
            QMessageBox.warning(
                self, "Binary mismatch",
                f"Binary too small for this XDF (needs ≥ 0x{maxend:X} bytes, file has 0x{len(data):X}).\n"
                "Wrong XDF (64K vs 512K) or wrong file?")
            return
        self.bin_path = path
        self.bin_data = data
        self._remember_dir('bin', path)
        self._push_recent('bin', path)
        self.lbl_bin.setText(os.path.basename(path))
        self.lbl_bin.setToolTip(path)
        self._read_tables_from_bin()
        self.log(f"Binary loaded: {os.path.basename(path)}  ({len(data) // 1024} KB, "
                 f"{'big' if self.tuner.big else 'little'}-endian, axes: "
                 f"MAP {self.tuner.map_ax.min():.0f}-{self.tuner.map_ax.max():.0f} kPa, "
                 f"RPM {self.tuner.rpm_ax.min():.0f}-{self.tuner.rpm_ax.max():.0f})")
        self.analysis_valid = False
        self._refresh_state()

    def _read_tables_from_bin(self):
        # resolve shared axes (linkobjid) through master table dict
        master = self.xdf_tables
        xaxis = resolve_axis(self.ve_tables[0].axes['x'], master)
        yaxis = resolve_axis(self.ve_tables[0].axes['y'], master)
        self.tuner.read_tables(self.bin_data, self.ve_tables, xaxis, yaxis)

    # ---- logs ----
    def add_logs(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select CSV log files",
            self._dirs.get('log') or "", "CSV logs (*.csv);;All files (*)")
        if not paths:
            return
        self._add_log_paths(paths)

    def _add_log_paths(self, paths):
        existing = {lg.path for lg in self.logs}
        for p in paths:
            if p in existing:
                continue
            prog = QProgressDialog(f"Parsing {os.path.basename(p)}…", "Cancel", 0, 0, self)
            prog.setWindowModality(Qt.WindowModal)
            QApplication.processEvents()
            lf = LogFile(p)
            prog.cancel()
            if lf.error or not lf.rows:
                self.log(f"!! Could not read {os.path.basename(p)}: {lf.error or 'no rows'}")
                continue
            self.logs.append(lf)
            self.list_logs.addItem(f"{os.path.basename(p)}   ({lf.rows:,} rows)")
            self.log(f"Log added: {os.path.basename(p)}  ({lf.rows:,} rows, {len(lf.header)} columns)")
            self._remember_dir('log', p)
            self._push_recent('log', p)
        self._auto_map_channels()
        self._refresh_state()

    def remove_log(self):
        for item in self.list_logs.selectedItems():
            i = self.list_logs.row(item)
            self.list_logs.takeItem(i)
            del self.logs[i]
        self._auto_map_channels()
        self._refresh_state()

    # ---- channels ----
    def _auto_map_channels(self):
        if not self.logs:
            return
        header = self.logs[0].header
        found = guess_channels(header)
        for k, cmb in self.channel_combos.items():
            cmb.blockSignals(True)
            cmb.clear()
            cmb.addItem("— not available —", None)
            for i, h in enumerate(header):
                cmb.addItem(h.strip() or f"col {i}", i)
            if k in found:
                cmb.setCurrentIndex(found[k] + 1)
            cmb.blockSignals(False)
        # auto map scale
        mi = found.get('map')
        if mi is not None:
            self.cmb_mapscale.setCurrentIndex(1 if guess_map_scale(header, mi) == 1.0 else 0)
        self._channels_changed()

    def _channels_ok(self):
        if not self.logs:
            return False
        for k in REQUIRED:
            cmb = self.channel_combos[k]
            if cmb.currentIndex() <= 0:
                return False
        afrsrc = self.cmb_afrsrc.currentIndex()
        if afrsrc != 1 and self.channel_combos['afr1'].currentIndex() <= 0:
            return False
        if afrsrc != 2 and self.channel_combos['afr2'].currentIndex() <= 0:
            return False
        return True

    def _channels_changed(self):
        self._refresh_state()

    def _collect_params(self):
        return {
            'rpm_min': self.sp_rpm_min.value(), 'rpm_max': self.sp_rpm_max.value(),
            'afr_min': self.sp_afr_min.value(), 'afr_max': self.sp_afr_max.value(),
            'use_inj': self.chk_inj.isChecked(), 'inj_min': self.sp_inj.value(),
            'use_clt': self.chk_clt.isChecked(), 'clt_min': self.sp_clt.value(),
            'use_transient': self.chk_trans.isChecked(),
            'trans_rpm': self.sp_trans_rpm.value(), 'trans_map': self.sp_trans_map.value(),
            'strength': self.sl_strength.value(), 'max_change': self.sp_maxchg.value(),
            'min_samples': self.sp_minsamp.value(), 'smoothing': self.sp_smooth.value(),
            'blend_tables': self.chk_blend.isChecked(),
        }

    def _params_debounced(self):
        self._param_debounce.start()

    def _reanalyze_if_valid(self):
        """Blend-mode toggle changes accumulation itself -> rerun full analysis."""
        if self.analysis_valid:
            self.analyze()

    def _params_changed(self):
        if self.analysis_valid:
            self._reapply()

    # ------------------------------------------------------------------ analyze
    def analyze(self):
        if not self._channels_ok():
            QMessageBox.warning(self, "Channels incomplete",
                                "Map all required log channels first (engine speed, manifold pressure, "
                                "active VE table, AFR target and at least one wideband AFR).")
            return
        samples, err = self._gather_samples()
        if samples is None:
            QMessageBox.critical(self, "Log error", err)
            return
        params = self._collect_params()
        self.tuner.analyze(samples, params)
        self.analysis_valid = True
        st = self.tuner.stats
        self.log(f"Analyzed {st['used']:,}/{st['total']:,} samples. Excluded: " +
                 ", ".join(f"{k}: {v:,}" for k, v in st['reasons'].items() if v) or "none")
        self._reapply(rebuild_tabs=True)

    def _gather_samples(self):
        n = sum(lg.rows for lg in self.logs)
        out = {k: np.empty(n) for k in ('rpm', 'mapkpa', 'avt', 'afr', 'target', 'trim')}
        optional = {k: np.full(n, np.nan) for k in ('inj', 'clt', 'time')}
        pos = 0
        for lg in self.logs:
            r = lg.rows
            sl = slice(pos, pos + r)
            pos += r
            for dst, key in (('rpm', 'rpm'), ('mapkpa', 'map'), ('avt', 'vetable'),
                             ('target', 'afrtarget')):
                ci = self.channel_combos[key].currentData()
                arr = lg.col(ci)
                if arr is None or len(arr) != r:
                    return None, f"Column missing in {os.path.basename(lg.path)}"
                out[dst][sl] = arr[:r] if len(arr) >= r else arr
            # map scaling
            mode = self.cmb_mapscale.currentText()
            scale = 1.0 if mode.endswith('kPa') else 100.0
            out['mapkpa'][sl] = out['mapkpa'][sl] * scale
            # AFR per bank
            src = self.cmb_afrsrc.currentIndex()
            a1i = self.channel_combos['afr1'].currentData()
            a2i = self.channel_combos['afr2'].currentData()
            a1 = lg.col(a1i) if a1i is not None else None
            a2 = lg.col(a2i) if a2i is not None else None
            if a1 is not None:
                a1 = np.where((a1[:r] > 5) & (a1[:r] < 25), a1[:r], np.nan)
            if a2 is not None:
                a2 = np.where((a2[:r] > 5) & (a2[:r] < 25), a2[:r], np.nan)
            if src == 1:
                out['afr'][sl] = a1 if a1 is not None else a2
            elif src == 2:
                out['afr'][sl] = a2 if a2 is not None else a1
            else:
                stack = [a for a in (a1, a2) if a is not None]
                if not stack:
                    return None, "No AFR channel available"
                if len(stack) == 2:
                    with np.errstate(invalid='ignore'):
                        both = np.nanmean(np.vstack(stack), axis=0)
                    both = np.where(np.isnan(both), np.nanmax(np.vstack(stack), axis=0), both)
                    out['afr'][sl] = both
                else:
                    out['afr'][sl] = stack[0]
            # trims
            if self.chk_trim.isChecked():
                acc = np.zeros(r)
                cnt = 0
                for k1, k2 in (('stft1', 'stft2'), ('ltft1', 'ltft2')):
                    for kk in (k1, k2):
                        ci = self.channel_combos[kk].currentData()
                        arr = lg.col(ci)
                        if arr is not None:
                            acc += np.nan_to_num(arr[:r])
                            cnt += 1
                out['trim'][sl] = acc if cnt else 0.0
            else:
                out['trim'][sl] = 0.0
            # optional channels
            for dst, key in (('inj', 'inj'), ('clt', 'clt'), ('time', 'time')):
                ci = self.channel_combos[key].currentData()
                arr = lg.col(ci)
                if arr is not None:
                    optional[dst][sl] = arr[:r]
        out.update(optional)
        # drop trim if all zero
        if np.allclose(out['trim'], 0):
            out['trim'] = None
        return out, None

    # ------------------------------------------------------------------ apply/view
    def _clear_tabs(self):
        while self.tabs.count() > 1:
            w = self.tabs.widget(1)
            self.tabs.removeTab(1)
            w.deleteLater()

    def _reapply(self, rebuild_tabs=False):
        if not self.analysis_valid:
            return
        params = self._collect_params()
        self.tuner.apply(params)
        self.apply_valid = True
        self.btn_write.setEnabled(True)
        self.btn_report.setEnabled(True)
        self.btn_revert.setEnabled(True)
        if rebuild_tabs:
            self._build_table_tabs()
        self._refresh_views()

    def _build_table_tabs(self):
        self._clear_tabs()
        self.ve_views = []
        self.ve_legends = []
        self.tab_headers = []
        for t, td in enumerate(self.tuner.tables):
            page = QWidget()
            v = QVBoxLayout(page)
            v.setContentsMargins(6, 6, 6, 6)
            top = QHBoxLayout()
            lbl = ElidedLabel()
            lbl.setStyleSheet(f"color: {C_TEXT_DIM}; font-size: 8.5pt;")
            top.addWidget(lbl, 1)
            mode = QComboBox()
            mode.addItem("Tuned VE", VeTableView.MODE_TUNED)
            mode.addItem("Original", VeTableView.MODE_ORIG)
            mode.addItem("Δ %", VeTableView.MODE_DELTA)
            mode.addItem("Factor", VeTableView.MODE_FACTOR)
            mode.addItem("Samples", VeTableView.MODE_SAMPLES)
            mode.setToolTip("Tuned VE: new values (editable) · Original: values in the loaded binary\n"
                            "Δ %: applied change · Factor: learned correction · Samples: data coverage")
            view = VeTableView()
            mode.currentIndexChanged.connect(
                lambda i, tt=t: self.set_view_mode(tt, i))
            top.addWidget(QLabel("View:"))
            top.addWidget(mode)
            legend = LegendBar()
            top.addWidget(legend)
            v.addLayout(top)
            v.addWidget(view, 1)
            self.tabs.addTab(page, f"VE {t + 1}")
            self.ve_views.append(view)
            self.ve_legends.append(legend)
            self.tab_headers.append(lbl)

    def set_view_mode(self, t, idx):
        view = self.ve_views[t]
        view.mode = idx
        view.refresh()
        wmax = float(view.data['wmax']) if view.data else 1.0
        self.ve_legends[t].set_mode(idx, self.sp_maxchg.value() / 100.0, wmax)

    def _refresh_views(self):
        factors = self.tuner.factors(self.sp_smooth.value())
        vmaxdisp = self.sp_maxchg.value() / 100.0
        for t, td in enumerate(self.tuner.tables):
            hit = self.tuner.W[t] >= self.sp_minsamp.value()
            d = {
                'rpm_ax': self.tuner.rpm_ax, 'map_ax': self.tuner.map_ax,
                'orig': self.tuner.orig[t], 'new': self.tuner.new[t],
                'new_raw': self.tuner.new_raw[t], 'fac': factors[t],
                'W': self.tuner.W[t], 'hit': hit,
                'vmax_display': vmaxdisp, 'wmax': max(self.tuner.W[t].max(), 1.0),
                'scale': td.scale, 'raw_lo': 0, 'raw_hi': 65535,
            }
            lo, hi = self.tuner.raw_bounds(td)
            d['raw_lo'], d['raw_hi'] = lo, hi
            if self.ve_views[t].data is None:
                self.ve_views[t].set_data(d)
            else:
                self.ve_views[t].data = d
                self.ve_views[t].refresh()
            ts = self.tuner.table_stats[t]
            short_desc = td.description.split(' - ')[-1].strip() if td.description else td.title
            txt = (f"{short_desc}  ·  {ts['samples']:.0f} samples  ·  "
                   f"{ts['tuned']}/{td.rows * td.cols} cells tuned  ·  "
                   f"Δ {ts['dmin'] * 100:+.0f}…{ts['dmax'] * 100:+.0f}%")
            self.tab_headers[t].setFullText(txt)
            self.tab_headers[t].setToolTip(
                f"{td.title}\n{td.description}\n\n"
                f"samples: {ts['samples']:.0f}   cells tuned: {ts['tuned']}   "
                f"factor mean: {ts['fac_mean']:.3f}   "
                f"Δ range: {ts['dmin'] * 100:+.1f}% … {ts['dmax'] * 100:+.1f}%")
            self.ve_legends[t].set_mode(self.ve_views[t].mode,
                                        self.sp_maxchg.value() / 100.0,
                                        float(self.tuner.W[t].max() or 1.0))
        self._refresh_overview()

    def _refresh_overview(self):
        st = self.tuner.stats
        self.lbl_overview.setText(
            f"<b>{st['used']:,}</b> of {st['total']:,} log samples used "
            f"({100.0 * st['used'] / max(st['total'], 1):.1f}%).   "
            + "   ".join(f"{k}: {v:,}" for k, v in st['reasons'].items() if v))
        ov = self.tbl_overview
        ov.setRowCount(len(self.tuner.tables))
        for t, td in enumerate(self.tuner.tables):
            ts = self.tuner.table_stats[t]
            vals = [f"VE {t + 1}", td.description or "—",
                    f"{ts['samples']:.0f}", f"{ts['hit']}", f"{ts['tuned']}",
                    f"{ts['fac_mean']:.3f}",
                    f"{ts['dmin'] * 100:+.0f}…{ts['dmax'] * 100:+.0f}%"]
            for c, val in enumerate(vals):
                it = QTableWidgetItem(val)
                if c >= 2:
                    it.setTextAlignment(Qt.AlignCenter)
                    it.setFont(QFont(MONO, 9))
                if c == 4 and ts['tuned'] == 0:
                    it.setForeground(QBrush(QColor(C_WARN)))
                ov.setItem(t, c, it)
        empty = [f"VE {t + 1}" for t, ts in enumerate(self.tuner.table_stats) if ts['samples'] == 0]
        self.lbl_overview2.setText(
            ("Tables with no log coverage: " + ", ".join(empty) +
             " (drive more / different VANOS conditions)") if empty else
            "All tables have log coverage.")
        self.lbl_axes.setText(
            f"RPM breakpoints: {' '.join(f'{int(v)}' for v in self.tuner.rpm_ax)}\n"
            f"MAP breakpoints (kPa): {' '.join(f'{v:.0f}' for v in self.tuner.map_ax)}")

    def revert_all(self):
        for t in range(len(self.tuner.tables)):
            self.tuner.new_raw[t] = self.tuner.orig_raw[t].copy()
            self.tuner.new[t] = self.tuner.orig[t].copy()
        self._refresh_views()
        self.log("All tables reverted to original values.")

    # ------------------------------------------------------------------ outputs
    def write_binary(self):
        if not self.apply_valid:
            return
        empty = [t + 1 for t, ts in enumerate(self.tuner.table_stats) if ts['samples'] == 0]
        tuned_cells = sum(ts['tuned'] for ts in self.tuner.table_stats)
        maxabs = max(max(abs(ts['dmin']), abs(ts['dmax'])) for ts in self.tuner.table_stats)
        msg = (f"{tuned_cells} cells will be updated across {len(self.tuner.tables)} tables.\n"
               f"Largest change: ±{maxabs * 100:.1f}%.\n"
               f"Output = copy of the original binary with VE tables patched.\n"
               f"Nothing else in the binary is modified.")
        if empty:
            msg += "\n\nNote: no data for tables " + ", ".join(f"VE{i}" for i in empty) + "."
        if QMessageBox.question(self, "Write tuned binary", msg) != QMessageBox.Yes:
            return
        suggest = os.path.splitext(os.path.basename(self.bin_path or "tuned.bin"))[0] + "_VEtuned.bin"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save tuned binary",
            os.path.join(os.path.dirname(self.bin_path or ""), suggest),
            "Binary (*.bin)")
        if not path:
            return
        if os.path.abspath(path) == os.path.abspath(self.bin_path):
            QMessageBox.warning(self, "Refusing to overwrite",
                                "Choose a different file name — the source binary is kept as backup.")
            return
        try:
            blob = self.tuner.write_binary(self.bin_data)
            _write_bytes_robust(path, blob)
        except OSError as e:
            QMessageBox.critical(self, "Write error",
                                 f"Could not write\n{path}\n\n{e}")
            return
        self.log(f"Tuned binary written: {path}  ({tuned_cells} cells updated)")
        QMessageBox.information(
            self, "Done",
            f"Tuned binary saved:\n{path}\n\nFlash it with your usual tools, "
            f"then log again and repeat until corrections approach ±3%.")

    def export_report(self):
        if not self.apply_valid:
            return
        suggest = os.path.splitext(os.path.basename(self.bin_path or "report"))[0] + "_VEtune_report.csv"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save CSV report",
            os.path.join(os.path.dirname(self.bin_path or ""), suggest), "CSV (*.csv)")
        if not path:
            return
        factors = self.tuner.factors(self.sp_smooth.value())
        buf = io.StringIO(newline='')
        wr = csv.writer(buf)
        wr.writerow(["table", "map_kpa", "rpm", "original", "new", "delta_pct",
                     "raw_original", "raw_new", "eff_samples", "corr_factor", "tuned"])
        for t, td in enumerate(self.tuner.tables):
            for rr in range(td.rows):
                for cc in range(td.cols):
                    old = self.tuner.orig[t][rr, cc]
                    new = self.tuner.new[t][rr, cc]
                    wr.writerow([
                        td.title, f"{self.tuner.map_ax[rr]:.1f}", int(self.tuner.rpm_ax[cc]),
                        f"{old:.4f}", f"{new:.4f}",
                        f"{(new / old - 1) * 100:+.2f}" if old else "0",
                        self.tuner.orig_raw[t][rr, cc], self.tuner.new_raw[t][rr, cc],
                        f"{self.tuner.W[t][rr, cc]:.2f}", f"{factors[t][rr, cc]:.4f}",
                        int(self.tuner.W[t][rr, cc] >= self.sp_minsamp.value())])
        try:
            _write_bytes_robust(path, buf.getvalue().encode('utf-8-sig'))
        except OSError as e:
            QMessageBox.critical(self, "Write error", f"Could not write\n{path}\n\n{e}")
            return
        self.log(f"Report written: {path}")
        QMessageBox.information(self, "Done", f"Report saved:\n{path}")


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("MS4X Tools")
    app.setStyleSheet(STYLESHEET)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
