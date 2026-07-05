#!/usr/bin/env python3
"""
Where do players actually move on the ground?  Aggregate grounded positions from
all of a map's pro demos into a top-down density heatmap, and overlay the bot's
current nav nodes -- so we can see the true walkable footprint and where the
bot's graph is missing coverage.

Usage:
    python demo_coverage.py [map] [raw_dir] [bot_nav]
"""

import glob
import math
import os
import struct
import sys
import zipfile
import zlib

import dm2parse

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RAW_DEFAULT = os.path.join(ROOT, "demos", "raw")
NAV_DEFAULT = os.path.join(ROOT, "engine", "ozbot", "nav", "q2dm1.nav")


def grounded_points(frames):
    """Yield (x,y,z) for frames where the player is on the ground (skip the
    ballistic part of jumps), matching the importer's ground detection."""
    if len(frames) < 3:
        return
    grounded, stable = True, 0
    yield frames[0]
    for i in range(1, len(frames)):
        vz = frames[i][2] - frames[i - 1][2]
        if grounded:
            if vz > 16:
                grounded, stable = False, 0
            else:
                yield frames[i]
        else:
            stable = stable + 1 if abs(vz) < 10 else 0
            if stable >= 2:
                grounded = True
                yield frames[i]


# ---- minimal PNG (8-bit RGB) ----
def write_png(path, w, h, px):
    def chunk(t, d):
        b = t + d
        return struct.pack(">I", len(d)) + b + struct.pack(">I", zlib.crc32(b) & 0xffffffff)
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw.extend(px[y * w * 3:(y + 1) * w * 3])
    out = b"\x89PNG\r\n\x1a\n"
    out += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    out += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    out += chunk(b"IEND", b"")
    open(path, "wb").write(out)


def heat(t):
    if t <= 0:
        return (8, 8, 16)
    stops = [(0, (10, 10, 50)), (.35, (0, 110, 200)), (.6, (0, 200, 160)),
             (.8, (140, 220, 60)), (1, (255, 235, 70))]
    for (a, ca), (b, cb) in zip(stops, stops[1:]):
        if t <= b:
            f = (t - a) / (b - a) if b > a else 0
            return tuple(int(ca[i] + (cb[i] - ca[i]) * f) for i in range(3))
    return stops[-1][1]


def read_nav_nodes(path):
    if not os.path.exists(path):
        return []
    d = open(path, "rb").read()
    magic, ver, n = struct.unpack_from("<iii", d, 0)
    o = 12
    pts = []
    for _ in range(n):
        x, y, z = struct.unpack_from("<fff", d, o); o += 12
        o += 1                       # flags
        (nl,) = struct.unpack_from("<i", d, o); o += 4
        o += nl * 12                 # links
        pts.append((x, y))
    return pts


def main(mapname, raw, navpath):
    # fixed q2dm1-ish world box (pad the known bounds)
    MINX, MAXX, MINY, MAXY = -250, 2150, -550, 1900
    SIZE, MARGIN = 512, 6
    span = max(MAXX - MINX, MAXY - MINY)
    gn = SIZE - 2 * MARGIN

    def to_px(x, y):
        gx = int((x - MINX) / span * (gn - 1)) + MARGIN
        gy = int((MAXY - y) / span * (gn - 1)) + MARGIN
        return gx, gy

    grid = [[0] * SIZE for _ in range(SIZE)]
    used = 0
    pts_total = 0
    for zp in sorted(glob.glob(os.path.join(raw, f"*{mapname}*.zip"))):
        try:
            z = zipfile.ZipFile(zp)
            dm2 = next((x for x in z.namelist() if x.lower().endswith(".dm2")), None)
            if not dm2:
                continue
            info = dm2parse.parse_data(z.read(dm2))
        except Exception:  # noqa: BLE001
            continue
        if info["map"] != mapname:
            continue
        used += 1
        for (x, y, _z) in grounded_points(info["frames"]):
            gx, gy = to_px(x, y)
            if 0 <= gx < SIZE and 0 <= gy < SIZE:
                grid[gy][gx] += 1
                pts_total += 1

    peak = max((max(r) for r in grid), default=1)
    lp = math.log1p(peak)
    px = bytearray(SIZE * SIZE * 3)
    for y in range(SIZE):
        for x in range(SIZE):
            c = grid[y][x]
            t = math.log1p(c) / lp if c and lp else 0
            r, g, b = heat(t)
            i = (y * SIZE + x) * 3
            px[i], px[i + 1], px[i + 2] = r, g, b

    # overlay bot nav nodes in magenta
    nodes = read_nav_nodes(navpath)
    for (x, y) in nodes:
        gx, gy = to_px(x, y)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                px_, py_ = gx + dx, gy + dy
                if 0 <= px_ < SIZE and 0 <= py_ < SIZE:
                    i = (py_ * SIZE + px_) * 3
                    px[i], px[i + 1], px[i + 2] = 255, 0, 255

    out = os.path.join(os.path.dirname(raw), f"{mapname}_coverage.png")
    write_png(out, SIZE, SIZE, px)
    print(f"demos used={used}  grounded points binned={pts_total}")
    print(f"bot nav nodes overlaid (magenta)={len(nodes)}")
    print(f"heat = player ground density; magenta = bot graph coverage")
    print(f"wrote {out}")


if __name__ == "__main__":
    mp = sys.argv[1] if len(sys.argv) > 1 else "q2dm1"
    raw = sys.argv[2] if len(sys.argv) > 2 else RAW_DEFAULT
    nav = sys.argv[3] if len(sys.argv) > 3 else NAV_DEFAULT
    main(mp, raw, nav)
