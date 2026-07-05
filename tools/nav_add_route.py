#!/usr/bin/env python3
"""
Merge demo-recorded routes into a .nav graph (targeted surgery).

Unlike the rejected wholesale demo->nav import (see the demo-import finding),
this adds ONE deliberately-walked route at a time: parse a cooperative demo
(recorded at walking pace, no trick jumps), sample its trajectory, and splice
the samples into an existing graph as nodes + links the bot's locomotion can
actually drive:

  - level / gentle-slope hops -> bidirectional WALK links
  - drops (>48u down)         -> one-way FALL links
  - climbs (>48u up)          -> skipped (lift rides; the bot can't drive a
                                 vertical column without plat support, and
                                 learned columns already exist where relevant)

Samples snap to existing nodes within SNAP units so the route stitches into
the graph instead of duplicating it.

Usage:
    py tools/nav_add_route.py <in.nav> <out.nav> <demo.dm2> [more.dm2 ...]
"""

import math
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dm2parse import parse  # noqa: E402

MAGIC = 0x56414E4F  # "ONAV"
MAX_LINKS = 8       # must match NAV_MAX_LINKS in bot_nav.h
SNAP = 40.0         # reuse an existing node within this range
H_STEP = 64.0       # sample spacing along the route (horizontal)
V_STEP = 40.0       # ...or on this much height change
WALK, JUMP, FALL, TELE, WATER = range(5)


def load_nav(path):
    with open(path, "rb") as f:
        magic, ver, n = struct.unpack("<iii", f.read(12))
        assert magic == MAGIC and ver == 1, "not a v1 ONAV file"
        nodes, links = [], []
        for _ in range(n):
            x, y, z = struct.unpack("<fff", f.read(12))
            flags = struct.unpack("<B", f.read(1))[0]
            nl = struct.unpack("<i", f.read(4))[0]
            ls = []
            for _ in range(nl):
                to = struct.unpack("<i", f.read(4))[0]
                typ = struct.unpack("<B", f.read(1))[0]
                f.read(3)
                cost = struct.unpack("<f", f.read(4))[0]
                ls.append([to, typ, cost])
            nodes.append([x, y, z, flags])
            links.append(ls)
    return nodes, links


def save_nav(path, nodes, links):
    with open(path, "wb") as f:
        f.write(struct.pack("<iii", MAGIC, 1, len(nodes)))
        for (x, y, z, flags), ls in zip(nodes, links):
            f.write(struct.pack("<fff", x, y, z))
            f.write(struct.pack("<B", flags))
            f.write(struct.pack("<i", len(ls)))
            for to, typ, cost in ls:
                f.write(struct.pack("<iB3xf", to, typ, cost))


def link_cost(a, b, typ):
    d = math.dist(a[:3], b[:3])
    if typ == FALL:
        return d * 1.2
    if typ == JUMP:
        return d * 1.5 + 32.0
    if typ == WATER:
        return d * 1.6
    return d


def add_link(nodes, links, a, b, typ):
    if a == b:
        return False
    for l in links[a]:
        if l[0] == b:
            return False
    if len(links[a]) >= MAX_LINKS:
        return False
    links[a].append([b, typ, link_cost(nodes[a], nodes[b], typ)])
    return True


def sample_route(frames):
    """Reduce the 10Hz trajectory to route samples."""
    pts = []
    for p in frames:
        if not pts:
            pts.append(p)
            continue
        last = pts[-1]
        if (math.hypot(p[0] - last[0], p[1] - last[1]) >= H_STEP
                or abs(p[2] - last[2]) >= V_STEP):
            pts.append(p)
    return pts


def merge_route(nodes, links, frames, label):
    added_nodes = added_links = snapped = skipped_climb = 0
    prev_idx = -1
    for p in sample_route(frames):
        # snap to an existing node, else append
        best, best_d = -1, SNAP
        for i, nd in enumerate(nodes):
            d = math.dist(p, (nd[0], nd[1], nd[2]))
            if d < best_d:
                best_d, best = d, i
        if best >= 0:
            idx = best
            snapped += 1
        else:
            nodes.append([p[0], p[1], p[2], 0])
            links.append([])
            idx = len(nodes) - 1
            added_nodes += 1

        if prev_idx >= 0 and prev_idx != idx:
            a, b = nodes[prev_idx], nodes[idx]
            dz = b[2] - a[2]
            hop = math.dist(a[:3], b[:3])
            if hop <= 200.0:
                if dz < -48:
                    added_links += add_link(nodes, links, prev_idx, idx, FALL)
                elif dz > 48:
                    skipped_climb += 1        # lift ride: not walkable
                else:
                    added_links += add_link(nodes, links, prev_idx, idx, WALK)
                    added_links += add_link(nodes, links, idx, prev_idx, WALK)
        prev_idx = idx
    print(f"{label}: +{added_nodes} nodes, +{added_links} links "
          f"({snapped} snapped to existing, {skipped_climb} climb hops skipped)")


def stitch(nodes, links, first_new):
    """Link added nodes to pre-existing neighbors at walkable height, so the
    spliced route actually joins the graph (e.g. an item's seeded node sitting
    just outside snap range)."""
    stitched = 0
    for i in range(first_new, len(nodes)):
        for j in range(first_new):
            dz = nodes[j][2] - nodes[i][2]
            if abs(dz) > 48:
                continue
            if math.dist(nodes[i][:3], nodes[j][:3]) > 72:
                continue
            stitched += add_link(nodes, links, i, j, WALK)
            stitched += add_link(nodes, links, j, i, WALK)
    print(f"stitch: +{stitched} links between new and existing nodes")


def main(argv):
    if len(argv) < 4:
        print(__doc__)
        return 2
    in_nav, out_nav = argv[1], argv[2]
    nodes, links = load_nav(in_nav)
    n0 = len(nodes)
    for demo in argv[3:]:
        info = parse(demo)
        merge_route(nodes, links, info["frames"], os.path.basename(demo))
    stitch(nodes, links, n0)
    save_nav(out_nav, nodes, links)
    print(f"{in_nav} ({n0} nodes) -> {out_nav} ({len(nodes)} nodes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
