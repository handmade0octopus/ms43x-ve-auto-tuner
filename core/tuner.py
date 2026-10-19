"""The 3D-histogram VE tuner.

Accumulates log evidence into a weighted 3D histogram over
(VE table / VANOS blend) x (MAP kPa) x (RPM), then derives per-cell
correction factors and new VE values.

Correction modes:
  'afr'      corr = AFR / AFR target                      (wideband only)
  'afr+trim' corr = 1 + trim%/100 where trim != 0, else AFR/target
             (trims have priority: a non-zero trim means the ECU is already
             compensating, so the trim carries the signal; the wideband
             fills in where the trims sit at 0 / open loop)
  'trim'     corr = 1 + trim%/100                          (lambda disabled,
             classic trim-based tuning; no AFR/target needed)
"""
import numpy as np


class Tuner:
    def __init__(self):
        self.tables = []          # list[TableDef]
        self.rpm_ax = None        # np.ndarray breakpoints
        self.map_ax = None
        self.big = False
        self.orig = []            # list[np.ndarray] scaled values
        self.orig_raw = []        # list[np.ndarray] int
        self.W = None             # (nt, r, c) effective sample weight  <- 3D histogram
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
            vals = np.frombuffer(bindata[xaxis.addr:xaxis.addr + 2 * xaxis.n],
                                 dtype=dt).astype(np.float64)
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

    # ---- analysis: build the 3D histogram ----
    def analyze(self, samples, params):
        """samples: dict of np arrays (rpm, mapkpa, avt, afr, target, trim).
        params: filter + mode options.  Fills W/C/E and stats."""
        nt = self.nt
        r = self.rpm_ax
        m = self.map_ax
        self.W = np.zeros((nt, len(m), len(r)))
        self.C = np.zeros_like(self.W)
        self.E = np.zeros_like(self.W)

        rpm = samples['rpm']; mp = samples['mapkpa']; avt = samples['avt']
        afr = samples.get('afr'); tgt = samples.get('target')
        trim = samples.get('trim')
        mode = params.get('corr_mode', 'afr')
        use_afr = mode in ('afr', 'afr+trim')

        reasons = {}
        mask = np.isfinite(rpm) & np.isfinite(mp) & np.isfinite(avt)
        if use_afr:
            mask &= np.isfinite(afr) & np.isfinite(tgt)
        reasons['missing values'] = int((~mask).sum())
        with np.errstate(invalid='ignore'):
            m2 = mask & (rpm >= params['rpm_min']) & (rpm <= params['rpm_max'])
        reasons['RPM out of range'] = int((mask & ~m2).sum()); mask = m2
        if use_afr:
            m2 = mask & (afr >= params['afr_min']) & (afr <= params['afr_max'])
            reasons['AFR implausible'] = int((mask & ~m2).sum()); mask = m2
            m2 = mask & (tgt >= 9.0) & (tgt <= 18.0)
            reasons['target implausible'] = int((mask & ~m2).sum()); mask = m2
        for rule in params.get('rules', []):
            vals = rule.get('vals')
            if vals is None or len(vals) != len(rpm):
                continue
            v = float(rule['value'])
            op = rule['op']
            with np.errstate(invalid='ignore'):
                if op == '<':
                    ok = vals < v
                elif op == '<=':
                    ok = vals <= v
                elif op == '>':
                    ok = vals > v
                elif op == '>=':
                    ok = vals >= v
                elif op == '==':
                    ok = vals == v
                elif op == '!=':
                    ok = vals != v
                else:
                    continue
            ok = ok & np.isfinite(vals)     # NaN never passes a rule
            m2 = mask & ok
            reasons[f"rule {rule['name']} {op} {v:g}"] = int((mask & ~m2).sum())
            mask = m2
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
            'corr_mode': mode,
        }
        if use_afr and reasons.get('AFR implausible', 0) > 0.9 * int(len(rpm)):
            self.stats['units_hint'] = ('Nearly all samples failed the AFR plausibility window. '
                                        'If your wideband logs LAMBDA, switch "wideband reads" to '
                                        'Lambda; if it logs AFR, check the mapped channels.')
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

        # per-sample correction factor
        if use_afr:
            corr = afr[mask] / tgt[mask]
            err = corr - 1.0
            if mode == 'afr+trim' and trim is not None:
                # trims have priority: where the ECU is actively trimming
                # (non-zero) the wideband reads the already-compensated
                # mixture, so the trim IS the correction; where trims sit at
                # zero (open loop) the wideband error is the truth.
                t = trim[mask]
                corr = np.where(np.isfinite(t) & (t != 0), 1.0 + t / 100.0, corr)
        else:
            t = trim[mask] if trim is not None else np.zeros(int(mask.sum()))
            t = np.nan_to_num(t, nan=0.0)
            corr = 1.0 + t / 100.0
            err = corr - 1.0

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

        # keep the histogram binning for per-parameter cell views
        self.stored = {
            'tlo': tlo, 'thi': thi, 'frac': frac,
            'jx': jx, 'jy': jy, 'fx': fx, 'fy': fy,
            'afr': afr[mask] if use_afr else None,
            'target': tgt[mask] if use_afr else None,
            'mask': mask,
        }

    def param_means(self, values):
        """Weighted per-cell mean of an arbitrary per-sample value array.

        values: np.ndarray aligned with the kept samples (len == mask.sum()).
        Returns (mean, weight) arrays shaped (nt, rows, cols)."""
        nt = self.nt
        mean = np.zeros_like(self.W)
        weight = np.zeros_like(self.W)
        tlo, thi = self.stored['tlo'], self.stored['thi']
        frac = self.stored['frac']
        jx, jy, fx, fy = self.stored['jx'], self.stored['jy'], self.stored['fx'], self.stored['fy']
        vals = np.asarray(values, dtype=np.float64)
        if len(vals) != len(tlo):
            return mean, weight
        for tt, tw in ((tlo, 1.0 - frac), (thi, frac)):
            if not np.any(tw > 0):
                continue
            for jr, wy in ((jy, 1 - fy), (jy + 1, fy)):
                for jc, wx in ((jx, 1 - fx), (jx + 1, fx)):
                    w = tw * wy * wx
                    np.add.at(weight, (tt, jr, jc), w)
                    np.add.at(mean, (tt, jr, jc), w * vals)
        with np.errstate(invalid="ignore", divide="ignore"):
            mean = np.where(weight > 0, mean / np.maximum(weight, 1e-9), 0.0)
        return mean, weight

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
        """Compute new table values from the 3D histogram and tuning params."""
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
