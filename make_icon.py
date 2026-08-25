# -*- coding: utf-8 -*-
"""生成应用图标 app.ico（粉色圆角方块 + 白色播放三角），纯标准库实现。"""
import struct
import zlib
from pathlib import Path

S = 256


def chunk(typ: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + typ + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))


def rounded_rect_sdf(x, y, x0, y0, x1, y1, r):
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 - r


def tri_sdf(px, py, a, b, c):
    def edge(u, v):
        ux, uy = u
        vx, vy = v
        ex, ey = vx - ux, vy - uy
        rx, ry = px - ux, py - uy
        denom = ex * ex + ey * ey
        t = max(0.0, min(1.0, (rx * ex + ry * ey) / denom)) if denom else 0.0
        qx, qy = ux + t * ex, uy + t * ey
        d = ((px - qx) ** 2 + (py - qy) ** 2) ** 0.5
        cross = ex * ry - ey * rx
        return d, cross

    d1, s1 = edge(a, b)
    d2, s2 = edge(b, c)
    d3, s3 = edge(c, a)
    inside = s1 < 0 and s2 < 0 and s3 < 0
    return (-1.0 if inside else 1.0) * min(d1, d2, d3)


def main():
    x0, y0, x1, y1 = 16, 16, 240, 240
    r = 56
    top, bottom = (255, 143, 177), (249, 101, 143)  # 粉色渐变
    tri = ((104, 88), (104, 168), (186, 128))        # 播放三角
    rows = []
    for y in range(S):
        row = bytearray()
        ty = y / (S - 1)
        pr = top[0] + (bottom[0] - top[0]) * ty
        pg = top[1] + (bottom[1] - top[1]) * ty
        pb = top[2] + (bottom[2] - top[2]) * ty
        for x in range(S):
            d = rounded_rect_sdf(x + 0.5, y + 0.5, x0, y0, x1, y1, r)
            a = max(0.0, min(1.0, 0.5 - d))
            if a <= 0:
                row += b"\x00\x00\x00\x00"
                continue
            td = tri_sdf(x + 0.5, y + 0.5, *tri)
            ta = max(0.0, min(1.0, 0.5 - td))
            cr = int(pr + (255 - pr) * ta)
            cg = int(pg + (255 - pg) * ta)
            cb = int(pb + (255 - pb) * ta)
            row += bytes((cr, cg, cb, int(a * 255)))
        rows.append(bytes(row))

    raw = b"".join(b"\x00" + rw for rw in rows)
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", S, S, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    ico = (struct.pack("<HHH", 0, 1, 1)
           + struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png), 22)
           + png)
    out = Path(__file__).resolve().parent / "app.ico"
    out.write_bytes(ico)
    print("written:", out, f"({len(ico)} bytes)")


if __name__ == "__main__":
    main()
