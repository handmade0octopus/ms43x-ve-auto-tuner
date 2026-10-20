"""CSV datalog loading and channel auto-detection."""
import csv
import io
import re

import numpy as np

from .robustio import read_bytes_robust


def _norm(s):
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', s.lower()).split())


CHANNEL_PATTERNS = {
    'rpm':       ['engine speed rpm', 'engine rpm', 'rpm', 'engine speed'],
    'map':       ['map bar', 'map kpa', 'map', 'manifold air pressure', 'manifold pressure'],
    'vetable':   ['active ve table u', 'active ve table', 've table', 'active ve'],
    'afr1':      ['afr 1 afr', 'afr1 afr', 'afr 1', 'afr1', 'wideband 1 afr', 'lambda 1',
                  'wideband afr 1', 'afr'],
    'afr2':      ['afr 2 afr', 'afr2 afr', 'afr 2', 'afr2', 'wideband 2 afr', 'lambda 2',
                  'wideband afr 2'],
    'afrtarget': ['air fuel ratio target', 'afr target', 'target afr', 'afr targ',
                  'lambda target'],
    'stft1':     ['stft 1', 'stft1', 'short term fuel trim bank 1'],
    'stft2':     ['stft 2', 'stft2', 'short term fuel trim bank 2'],
    'ltft1':     ['ltft 1', 'ltft1', 'long term fuel trim bank 1',
                  'long term fuel trim multiplicative', 'ltft multiplicative'],
    'ltft2':     ['ltft 2', 'ltft2', 'long term fuel trim multiplicative bank 2',
                  'ltft 2 multiplicative'],
    'inj':       ['injection time', 'fuel inj ms', 'fuel inj', 'injection time ms', 'inj time'],
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

# Bare 'afr' must only match an exact column name, otherwise it would also
# grab 'AFR target' or 'AFR 2' columns when the bank-1 channel is missing.
_EXACT_ONLY = {'afr'}


def _matches(pattern, normed, unitn, exact_only):
    """Find a column by name (header first, then the units row if present)."""
    hit = next((i for i, nh in enumerate(normed) if nh == pattern), None)
    if hit is None and unitn:
        hit = next((i for i, uh in enumerate(unitn) if uh == pattern), None)
    if hit is not None or exact_only:
        return hit
    hit = next((i for i, nh in enumerate(normed) if pattern in nh), None)
    if hit is None and unitn:
        hit = next((i for i, uh in enumerate(unitn) if pattern in uh), None)
    return hit


def guess_channels(header, units=None):
    """Auto-map known channel names. ``units`` (optional) is the units row that
    TunerPro exports under the header; it helps when names lack unit suffixes."""
    normed = [_norm(h) for h in header]
    unitn = [_norm(u) for u in units] if units else []
    if len(unitn) != len(normed):
        unitn = []
    found = {}
    for key, pats in CHANNEL_PATTERNS.items():
        for p in pats:
            hit = _matches(p, normed, unitn, p in _EXACT_ONLY)
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


def guess_map_scale(header, col, units=None):
    if col is None:
        return 100.0

    def scale(text):
        u = _norm(text)
        if 'kpa' in u:
            return 1.0
        if 'bar' in u:
            return 100.0
        return None

    if col < len(header):
        s = scale(header[col])
        if s is not None:
            return s
    if units and col < len(units):
        s = scale(units[col])
        if s is not None:
            return s
    return 100.0


class LogFile:
    """One parsed CSV log: numeric columns cached as float arrays."""
    def __init__(self, path=None, *, name=None, data=None):
        """Desktop: path on disk. Web: pass name + data (bytes, never stored)."""
        self.path = name if name is not None else path
        self._bytes = data
        self.header = []
        self.units = []           # optional units row (TunerPro exports one)
        self.rows = 0
        self.data = None          # dict col_idx -> np.ndarray
        self.error = None
        self._parse()

    def _parse(self):
        try:
            raw = self._bytes if self._bytes is not None else read_bytes_robust(self.path)
            text = raw.decode('utf-8-sig', errors='replace')
            rdr = csv.reader(io.StringIO(text, newline=''))
            rows = iter(rdr)
            # Some exporters (TunerPro) write a free-text title line before the
            # real header, e.g. "TunerPro Engine data log recorded on ...".
            # The header is the first row with at least two non-empty cells.
            for row in rows:
                if sum(1 for c in row if c.strip()) >= 2:
                    self.header = [c.strip() for c in row]
                    break
            ncols = len(self.header)
            cols = [[] for _ in range(ncols)]
            n = 0
            first = True
            for row in rows:
                if not row or all(not c.strip() for c in row):
                    continue
                vals = []
                numeric = False
                for i in range(ncols):
                    v = row[i] if i < len(row) else ''
                    try:
                        fv = float(v) if v.strip() else np.nan
                        numeric = numeric or bool(v.strip())
                    except ValueError:
                        fv = np.nan
                    vals.append(fv)
                if not numeric:
                    # A units row directly under the header has no numbers.
                    if first:
                        self.units = [(row[i] if i < len(row) else '').strip()
                                      for i in range(ncols)]
                    first = False
                    continue
                first = False
                for i, fv in enumerate(vals):
                    cols[i].append(fv)
                n += 1
            self.rows = n
            self.data = {i: np.asarray(c, dtype=np.float64) for i, c in enumerate(cols)}
        except Exception as e:  # noqa: BLE001
            self.error = str(e)

    def col(self, idx):
        return self.data.get(idx)

    def header_matches(self, other):
        return [h.strip() for h in self.header] == [h.strip() for h in other.header]
