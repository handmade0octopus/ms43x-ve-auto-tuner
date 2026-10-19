#!/usr/bin/env python3
"""Generate icon.png + icon.ico for MS43X VE auto-tuner (run once, output committed)."""
import struct
import sys

from PyQt5.QtCore import Qt, QRect
from PyQt5.QtGui import QPixmap, QPainter, QPen, QColor, QFont
from PyQt5.QtWidgets import QApplication


def render(size):
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    m = size / 256.0
    # rounded plate
    p.setPen(QPen(QColor('#2f81f7'), int(10 * m)))
    p.setBrush(QColor('#21252b'))
    p.drawRoundedRect(int(8 * m), int(8 * m), int(240 * m), int(240 * m),
                      int(48 * m), int(48 * m))
    # gauge arc: green sweep then red zone
    p.setPen(QPen(QColor('#3fb950'), int(11 * m), Qt.SolidLine, Qt.RoundCap))
    p.drawArc(QRect(int(48 * m), int(38 * m), int(160 * m), int(160 * m)), 200 * 16, -140 * 16)
    p.setPen(QPen(QColor('#f85149'), int(11 * m), Qt.SolidLine, Qt.RoundCap))
    p.drawArc(QRect(int(48 * m), int(38 * m), int(160 * m), int(160 * m)), 60 * 16, -40 * 16)
    # needle
    import math
    ang = math.radians(-20)
    cx, cy = 128 * m, 118 * m
    p.setPen(QPen(QColor('#d7dde4'), int(7 * m), Qt.SolidLine, Qt.RoundCap))
    p.drawLine(int(cx), int(cy), int(cx + 68 * m * math.cos(ang)), int(cy - 68 * m * math.sin(ang)))
    p.setPen(QPen(QColor('#d7dde4'), int(4 * m)))
    p.setBrush(QColor('#d7dde4'))
    p.drawEllipse(int(cx - 8 * m), int(cy - 8 * m), int(16 * m), int(16 * m))
    # VE text
    f = QFont('Segoe UI', int(64 * m), QFont.Bold)
    p.setFont(f)
    p.setPen(QColor('#ffffff'))
    p.drawText(QRect(0, int(126 * m), size, int(94 * m)), Qt.AlignHCenter, "VE")
    p.end()
    return pm


def png_to_ico(png_bytes, sizes=(16, 24, 32, 48, 64, 128, 256)):
    """Wrap PNG images into a .ico container (PNG-compressed entries, Vista+)."""
    from PyQt5.QtCore import QBuffer
    images = []
    for s in sizes:
        img = render(s).toImage()
        buf = QBuffer()
        buf.open(QBuffer.ReadWrite)
        img.save(buf, 'PNG')
        images.append((s, bytes(buf.data())))
    n = len(images)
    out = struct.pack('<HHH', 0, 1, n)
    offset = 6 + 16 * n
    entries = b''
    data = b''
    for s, blob in images:
        w = 0 if s >= 256 else s
        h = 0 if s >= 256 else s
        entries += struct.pack('<BBBBHHII', w, h, 0, 0, 1, 32, len(blob), offset)
        data += blob
        offset += len(blob)
    return out + entries + data


if __name__ == "__main__":
    app = QApplication([])
    big = render(256)
    big.save("icon.png")
    ico = png_to_ico(None)
    with open("icon.ico", "wb") as f:
        f.write(ico)
    print("icon.png + icon.ico written")
