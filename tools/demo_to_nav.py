#!/usr/bin/env python3
"""
ozbot demo -> navmesh importer.

Reads pro .dm2 demos for a map (straight from the downloaded zips), extracts the
recording player's movement trajectory, and builds a nav graph using the SAME
node/link rules as the in-game learner (bot_nav.c) -- then writes it in the
exact binary .nav format the DLL's Nav_Load expects.  The result is a far
better-covered, human-validated graph than bots produce by wandering.

(Item nodes are seeded by the DLL at map load, so we don't add them here.)

Usage:
    python demo_to_nav.py <map> [raw_dir] [out.nav]
    e.g. python demo_to_nav.py q2dm1
"""

import glob
import math
import os
import struct
import sys
import zipfile

import dm2parse

# must match bot_nav.h / bot_nav.c
NAV_MAGIC = 0x56414E4F      # 'O','N','A','V' little-endian
NAV_VERSION = 1
MAX_NODES = 2048
MAX_LINKS = 8
NODE_DENSITY = 96.0
LINK_MAX_DIST = 200.0
Z_MERGE = 48.0
CONSERV_DIST = 128.0    # drop momentum strides longer than this
CONSERV_UP = 40.0       # drop links that climb more than this (likely jumps)
WALK, FALL, JUMP, TELEPORT, WATER = 0, 1, 2, 3, 4

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RAW_DEFAULT = os.path.join(ROOT, "demos", "raw")


class Graph:
    def __init__(self):
        self.nodes = []                 # [x, y, z, flags, links[(to,type,cost)]]
        self.grid = {}                  # (gx,gy) -> [node indices]

    def _cell(self, x, y):
        return (int(math.floor(x / NODE_DENSITY)), int(math.floor(y / NODE_DENSITY)))

    def add_node(self, x, y, z):
        best, bestd = -1, NODE_DENSITY * NODE_DENSITY
        cx, cy = self._cell(x, y)
        for gx in (cx - 1, cx, cx + 1):
            for gy in (cy - 1, cy, cy + 1):
                for i in self.grid.get((gx, gy), ()):
                    nx, ny, nz = self.nodes[i][0:3]
                    if abs(z - nz) > Z_MERGE:
                        continue
                    d = (x - nx) ** 2 + (y - ny) ** 2 + (z - nz) ** 2
                    if d < bestd:
                        bestd, best = d, i
        if best >= 0:
            return best
        if len(self.nodes) >= MAX_NODES:
            return -1
        idx = len(self.nodes)
        self.nodes.append([x, y, z, 0, []])
        self.grid.setdefault((cx, cy), []).append(idx)
        return idx

    def _cost(self, a, b, t):
        d = math.dist(self.nodes[a][0:3], self.nodes[b][0:3])
        return {FALL: d * 1.2, JUMP: d * 1.5 + 32, TELEPORT: 64.0,
                WATER: d * 1.6}.get(t, d)

    def add_link(self, a, b, t):
        if a < 0 or b < 0 or a == b:
            return
        links = self.nodes[a][4]
        for l in links:
            if l[0] == b:
                return
        if len(links) >= MAX_LINKS:
            return
        links.append((b, t, self._cost(a, b, t)))

    def _walk_link(self, a, b):
        # Use demo trajectories for NODE COVERAGE, keeping only links a simple bot
        # can traverse: flat/ramp walks and drop-downs.  We exclude up-jumps and
        # long momentum strides (pro movement the bot can't reproduce); the bot
        # learns level-changes/jumps itself at runtime.
        if a < 0 or b < 0 or a == b:
            return
        if math.dist(self.nodes[a][0:3], self.nodes[b][0:3]) > CONSERV_DIST:
            return
        dz = self.nodes[b][2] - self.nodes[a][2]
        if dz > CONSERV_UP:
            return                              # likely a jump up -- skip
        if dz < -40:
            self.add_link(a, b, FALL)           # drop down -- one-way, bot-safe
        else:
            self.add_link(a, b, WALK)           # flat / ramp -- bidirectional
            self.add_link(b, a, WALK)

    def add_trajectory(self, frames):
        """Place nodes only where the player was on the ground, and turn jumps/
        falls into one-way JUMP/FALL links -- so the graph encodes movement the
        BOT can execute, not pro airborne tricks (rocket-jumps are dropped).

        A frame is treated as airborne after a sharp upward velocity (a jump);
        we wait until vertical velocity settles for a couple of frames (landed)
        before resuming ground nodes."""
        if len(frames) < 3:
            return
        TAKEOFF_VZ = 16.0       # >~160 ups upward in a frame == a jump
        LAND_STABLE = 10.0      # |vz| below this == not climbing/falling
        MAX_JUMP_DZ = 72.0      # height a bot jump can gain; bigger == rocket-jump

        grounded = True
        last_node = self.add_node(*frames[0])
        takeoff_node, takeoff_z = -1, 0.0
        stable = 0

        for i in range(1, len(frames)):
            x, y, z = frames[i]
            vz = z - frames[i - 1][2]

            if grounded:
                if vz > TAKEOFF_VZ:
                    grounded = False
                    takeoff_node, takeoff_z = last_node, frames[i - 1][2]
                    stable = 0
                    continue
                cur = self.add_node(x, y, z)
                if cur >= 0:
                    self._walk_link(last_node, cur)
                    last_node = cur
            else:
                stable = stable + 1 if abs(vz) < LAND_STABLE else 0
                if stable >= 2:                 # landed
                    grounded = True
                    cur = self.add_node(x, y, z)        # keep the landing spot as
                    if cur >= 0:                        # coverage, but DON'T import
                        last_node = cur                 # the jump/fall as a link --
                        # the bot learns level-changes itself (and can't reliably
                        # do pro jumps), so importing them just makes it stall.

    def write(self, path):
        with open(path, "wb") as f:
            f.write(struct.pack("<iii", NAV_MAGIC, NAV_VERSION, len(self.nodes)))
            for (x, y, z, flags, links) in self.nodes:
                f.write(struct.pack("<fff", x, y, z))
                f.write(struct.pack("<B", flags))
                f.write(struct.pack("<i", len(links)))
                for (to, t, c) in links:
                    # nav_link_t = {int to; byte type; <3 pad>; float cost} = 12 bytes
                    f.write(struct.pack("<iBxxxf", to, t, c))


def main(mapname, raw_dir, out):
    g = Graph()
    # filename hint narrows the work; the demo's own map field is authoritative
    cand = sorted(glob.glob(os.path.join(raw_dir, f"*{mapname}*.zip")))
    used = skipped = errs = total_frames = 0
    for zp in cand:
        try:
            z = zipfile.ZipFile(zp)
            dm2 = next((n for n in z.namelist() if n.lower().endswith(".dm2")), None)
            if not dm2:
                continue
            info = dm2parse.parse_data(z.read(dm2))
        except Exception as e:  # noqa: BLE001
            errs += 1
            continue
        if info["map"] != mapname or not info["frames"]:
            skipped += 1
            continue
        g.add_trajectory(info["frames"])
        used += 1
        total_frames += len(info["frames"])
        if used % 25 == 0:
            print(f"  ...{used} demos, {len(g.nodes)} nodes", flush=True)

    links = sum(len(n[4]) for n in g.nodes)
    g.write(out)
    print(f"map={mapname}")
    print(f"demos matched/used={used}  skipped(other map)={skipped}  errors={errs}")
    print(f"frames processed={total_frames}")
    print(f"nodes={len(g.nodes)}  links={links}")
    if g.nodes:
        xs = [n[0] for n in g.nodes]; ys = [n[1] for n in g.nodes]; zs = [n[2] for n in g.nodes]
        print(f"bounds x {min(xs):.0f}..{max(xs):.0f}  y {min(ys):.0f}..{max(ys):.0f}  "
              f"z {min(zs):.0f}..{max(zs):.0f}")
    print(f"wrote {out}")


if __name__ == "__main__":
    mp = sys.argv[1] if len(sys.argv) > 1 else "q2dm1"
    raw = sys.argv[2] if len(sys.argv) > 2 else RAW_DEFAULT
    outp = sys.argv[3] if len(sys.argv) > 3 else \
        os.path.join(os.path.dirname(raw), f"{mp}_from_demos.nav")
    main(mp, raw, outp)
