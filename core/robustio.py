"""Retry-hardened file IO (transient Windows handle errors: cloud sync, AV, dialogs)."""
import time

# transient Windows handle states (cloud-sync placeholders, AV scans, dialog locks)
RETRYABLE_WINERRORS = {6, 22, 32, 121, 1237}


def _read_bytes_with(opener, path, tries=5, base_delay=0.15):
    """Read a file, retrying transient Windows handle errors (WinError 6/32/...)."""
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


def read_bytes_robust(path, **kw):
    return _read_bytes_with(open, path, **kw)


def write_bytes_robust(path, data, tries=5, base_delay=0.15):
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
