#!/usr/bin/env python3
"""
ozbot goal-attempt miner (Track E).

Reconstructs every goal attempt from telemetry by pairing each goal-commit
event (goal_item / goal) with its terminal event (pickup / item_lost / reach /
giveup / pathfail / death), per bot.  This splits the failure population by
goal kind (item vs roam) and by item name -- something analyze.py's flat event
counts can't do -- and prints:

  - outcome tables by goal kind (counts, time spent, median durations)
  - per-item attempts / pickups / completion% / failure mix
  - giveup deep-dive (vertical split, closest-approach, path progress,
    at-node share, fighting share) separately for item and roam attempts
  - giveup hotspot clusters with mean vertical miss (gvdist)
  - explore-gap stats (time bots spend between attempts)

Usage:
    python tools/mine_goals.py <telemetry.jsonl> [more.jsonl ...]
"""

import json
import math
import sys
from collections import defaultdict

TERMINALS = ("pickup", "item_lost", "reach", "giveup", "pathfail", "death")


def load_events(paths):
    events = []
    for fi, path in enumerate(paths):
        with open(path, "r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") == "event":
                    rec["_ord"] = len(events)
                    rec["_file"] = fi     # bot ids and t restart per file
                    events.append(rec)
    return events


def build_attempts(events):
    """Pair goal commits with terminals, per bot.  Returns (attempts, anomalies)."""
    by_bot = defaultdict(list)
    for e in events:
        by_bot[(e["_file"], e.get("bot", -1))].append(e)

    attempts, anomalies = [], defaultdict(int)
    for bot, evs in by_bot.items():
        evs.sort(key=lambda e: (e["t"], e["_ord"]))
        open_at = None
        for e in evs:
            ev = e.get("event")
            if ev in ("goal_item", "goal"):
                if open_at is not None:
                    # shouldn't happen (every exit path logs a terminal)
                    anomalies["preempted"] += 1
                    open_at["outcome"] = "preempted"
                    open_at["end_t"] = e["t"]
                    attempts.append(open_at)
                open_at = {
                    "bot": bot,
                    "kind": "item" if ev == "goal_item" else "roam",
                    "item": e.get("item", ""),
                    "start_t": e["t"],
                    "start_xyz": (e.get("x"), e.get("y"), e.get("z")),
                }
            elif ev in TERMINALS:
                if open_at is None:
                    if ev != "death":          # deaths outside attempts are normal
                        anomalies["orphan_" + ev] += 1
                    continue
                open_at["outcome"] = ev
                open_at["end_t"] = e["t"]
                open_at["end_xyz"] = (e.get("x"), e.get("y"), e.get("z"))
                if ev == "giveup":
                    for k in ("gdist", "gvdist", "atnode", "fighting",
                              "pidx", "plen", "gbest"):
                        open_at[k] = e.get(k)
                attempts.append(open_at)
                open_at = None
        if open_at is not None:
            open_at["outcome"] = "unresolved"   # map/log ended mid-attempt
            open_at["end_t"] = evs[-1]["t"]
            attempts.append(open_at)
    return attempts, anomalies


def median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else 0.0


def pct(n, d):
    return f"{100.0 * n / d:.0f}%" if d else "-"


def cluster(pts, radius=150.0, min_n=2):
    """Greedy 2D clustering (same idea as analyze.py's hotspots)."""
    clusters = []                      # [cx, cy, n, sum_z, sum_gv]
    for (x, y, z, gv) in pts:
        for c in clusters:
            if math.hypot(x - c[0], y - c[1]) < radius:
                n = c[2]
                c[0] = (c[0] * n + x) / (n + 1)
                c[1] = (c[1] * n + y) / (n + 1)
                c[2] += 1
                c[3] += z
                c[4] += gv
                break
        else:
            clusters.append([x, y, 1, z, gv])
    out = [(c[2], c[0], c[1], c[3] / c[2], c[4] / c[2])
           for c in clusters if c[2] >= min_n]
    out.sort(key=lambda c: -c[0])
    return out


def outcome_table(title, rows):
    """rows: list of attempts of one kind."""
    n = len(rows)
    if not n:
        return
    by_out = defaultdict(list)
    for a in rows:
        by_out[a["outcome"]].append(a)
    tot_time = sum(a["end_t"] - a["start_t"] for a in rows)
    print(f"\n{title} (n={n}, {tot_time:.0f}s total in-attempt)")
    print(f"   {'outcome':<11} {'n':>5} {'share':>6} {'med-dur':>8} {'time':>7} {'t-share':>7}")
    order = ("pickup", "reach", "item_lost", "giveup", "pathfail", "death",
             "preempted", "unresolved")
    for out in order:
        rs = by_out.get(out)
        if not rs:
            continue
        durs = [a["end_t"] - a["start_t"] for a in rs]
        tt = sum(durs)
        print(f"   {out:<11} {len(rs):>5} {pct(len(rs), n):>6} "
              f"{median(durs):>7.1f}s {tt:>6.0f}s {pct(tt, tot_time):>7}")


def giveup_report(title, gvs):
    n = len(gvs)
    if not n:
        return
    above = sum(1 for a in gvs if (a.get("gvdist") or 0) > 32)
    below = sum(1 for a in gvs if (a.get("gvdist") or 0) < -32)
    level = n - above - below
    near = sum(1 for a in gvs if (a.get("gdist") or 9999) < 80)
    atnode = sum(1 for a in gvs if a.get("atnode"))
    fighting = sum(1 for a in gvs if a.get("fighting"))
    reached = sum(1 for a in gvs if (a.get("gbest") or 9999) < 60)
    prog = [100.0 * a["pidx"] / a["plen"] for a in gvs if a.get("plen")]
    print(f"\n{title} (n={n})")
    print(f"   vertical: above(>32u)={pct(above, n)}  level={pct(level, n)}  "
          f"below(<-32u)={pct(below, n)}")
    print(f"   within-80u-of-goal={pct(near, n)}  median-dist="
          f"{median([a.get('gdist') or 0 for a in gvs]):.0f}u  "
          f"at-node(path done)={pct(atnode, n)}")
    print(f"   closest-to-node: median={median([a.get('gbest') or 9999 for a in gvs]):.0f}u  "
          f"ever-reached(<60u)={pct(reached, n)}  fighting={pct(fighting, n)}")
    if prog:
        print(f"   path progress: median={median(prog):.0f}% of waypoints")
    pts = [(a["end_xyz"][0], a["end_xyz"][1], a["end_xyz"][2],
            a.get("gvdist") or 0) for a in gvs if a.get("end_xyz")]
    if pts:
        print("   hotspots (n @ x,y | mean-z | mean gvdist):")
        for cn, cx, cy, cz, cgv in cluster(pts)[:8]:
            print(f"     {cn:>3} @ ({cx:>7.0f},{cy:>7.0f}) | z~{cz:>5.0f} | "
                  f"item {cgv:+.0f}u vertical")


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    events = load_events(argv[1:])
    attempts, anomalies = build_attempts(events)
    if anomalies:
        print("anomalies: " + ", ".join(f"{v} {k}" for k, v in anomalies.items()))

    items = [a for a in attempts if a["kind"] == "item"]
    roams = [a for a in attempts if a["kind"] == "roam"]

    outcome_table("ITEM attempts", items)
    outcome_table("ROAM attempts", roams)

    # per-item table
    by_item = defaultdict(list)
    for a in items:
        by_item[a["item"] or "?"].append(a)
    print(f"\nper-item (attempts / pickups / ITEM% / giveup / lost / died / med-pickup-s):")
    for name, rs in sorted(by_item.items(), key=lambda kv: -len(kv[1])):
        outs = defaultdict(int)
        for a in rs:
            outs[a["outcome"]] += 1
        pk = [a["end_t"] - a["start_t"] for a in rs if a["outcome"] == "pickup"]
        print(f"   {name:<16} {len(rs):>4} {outs['pickup']:>5} "
              f"{pct(outs['pickup'], len(rs)):>5}  {outs['giveup']:>4} "
              f"{outs['item_lost']:>5} {outs['death']:>4} {median(pk):>6.1f}s")

    giveup_report("ITEM giveup deep-dive",
                  [a for a in items if a["outcome"] == "giveup"])
    giveup_report("ROAM giveup deep-dive",
                  [a for a in roams if a["outcome"] == "giveup"])

    # per-item giveup verticality (which items are the vertical failures?)
    gv_items = [a for a in items if a["outcome"] == "giveup"]
    by_item_gv = defaultdict(list)
    for a in gv_items:
        by_item_gv[a["item"] or "?"].append(a.get("gvdist") or 0)
    if by_item_gv:
        print("\ngiveups by item (n | above/level/below | median gvdist):")
        for name, gvl in sorted(by_item_gv.items(), key=lambda kv: -len(kv[1])):
            ab = sum(1 for v in gvl if v > 32)
            be = sum(1 for v in gvl if v < -32)
            print(f"   {name:<16} {len(gvl):>4} | {ab}/{len(gvl)-ab-be}/{be} | "
                  f"{median(gvl):+5.0f}u")

    # explore gaps: time between a terminal and the bot's next goal commit
    gaps = []
    by_bot = defaultdict(list)
    for a in attempts:
        by_bot[a["bot"]].append(a)
    for bot, rs in by_bot.items():
        rs.sort(key=lambda a: a["start_t"])
        for prev, cur in zip(rs, rs[1:]):
            if prev.get("end_t") is not None and prev["outcome"] != "death":
                gaps.append(cur["start_t"] - prev["end_t"])
    if gaps:
        tot_attempt = sum(a["end_t"] - a["start_t"] for a in attempts)
        tot_gap = sum(gaps)
        print(f"\nexplore gaps between attempts: n={len(gaps)} "
              f"median={median(gaps):.1f}s mean={tot_gap/len(gaps):.1f}s")
        print(f"   time split: {tot_attempt:.0f}s in attempts vs "
              f"{tot_gap:.0f}s between = "
              f"{pct(tot_gap, tot_attempt + tot_gap)} of goal-cycle time exploring")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
