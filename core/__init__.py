"""MS43X VE auto-tuner core: XDF parsing, log loading, 3D-histogram VE tuner."""
from .xdf import AxisDef, TableDef, parse_xdf, parse_xdf_data, find_ve_family, resolve_axis
from .logs import LogFile, guess_channels, guess_map_scale, CHANNEL_PATTERNS, CHANNEL_LABELS
from .tuner import Tuner
from .robustio import read_bytes_robust, write_bytes_robust

__all__ = [
    "AxisDef", "TableDef", "parse_xdf", "parse_xdf_data", "find_ve_family", "resolve_axis",
    "LogFile", "guess_channels", "guess_map_scale", "CHANNEL_PATTERNS", "CHANNEL_LABELS",
    "Tuner", "read_bytes_robust", "write_bytes_robust",
]
