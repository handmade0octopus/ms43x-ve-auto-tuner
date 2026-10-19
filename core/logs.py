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
    def __init__(self, path=None, *, name=None, data=None):
        """Desktop: path on disk. Web: pass name + data (bytes, never stored)."""
        self.path = name if name is not None else path
        self._bytes = data
        self.header = []
        self.rows = 0
        self.data = None          # dict col_idx -> np.ndarray
        self.error = None
        self._parse()

    def _parse(self):
        try:
            raw = self._bytes if self._bytes is not None else read_bytes_robust(self.path)
            text = raw.decode('utf-8-sig', errors='replace')
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

    def header_matches(self, other):
        return [h.strip() for h in self.header] == [h.strip() for h in other.header]
