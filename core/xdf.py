"""TunerPro XDF parsing (tables, constants skipped; shared axes resolved via linkobjid)."""
import re
import xml.etree.ElementTree as ET


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
            import numpy as np
            return np.array(self.labels, dtype=float)
        if self.addr is None or not self.n:
            return None
        import numpy as np
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
