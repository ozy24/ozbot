#!/usr/bin/env python3
"""Mine ozbot telemetry for goal-indecision: "standing re-decides".

A standing re-decide is a gap between a goal EXIT event (pickup / item_lost /
giveup / reach / pathfail) and the next goal COMMIT event (goal_item / goal)
for the same bot, lasting >= 1s with < 64u horizontal displacement -- the bot
visibly stands there re-deciding (often swinging its view between candidate
items) instead of moving.  Built to validate the bot_decisive fix; see the
ozbot-decisive memory / PLAN.md.

Usage:  py tools/mine_indecision.py <merged_log.jsonl> [<log2.jsonl> ...]
Multiple logs are each summarized separately (e.g. an ON and an OFF arm).
"""
import json
import sys
import math
import collections
import statistics

EXIT_EVENTS = {"giveup", "reach", "pathfail", "item_lost", "pickup"}
COMMIT_EVENTS = {"goal_item", "goal"}
STAND_MIN_DT = 1.0      # seconds
STAND_MAX_DISP = 64.0   # units


def yaw_delta(a, b):
    d = b - a
    while d > 180:
        d -= 360
    while d < -180:
        d += 360
    return d


def analyze(path):
    ticks = collections.defaultdict(list)
    events = collections.defaultdict(list)
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = r.get("type")
            if t == "tick":
                ticks[r["bot"]].append(r)
            elif t == "event":
                if "bot" not in r:
                    continue    # world events (item respawns) aren't per-bot
                events[r["bot"]].append(r)

    gaps = []
    for bot, evs in events.items():
        evs.sort(key=lambda r: r["t"])
        for i in range(len(evs) - 1):
            a, b = evs[i], evs[i + 1]
            if a["event"] in EXIT_EVENTS and b["event"] in COMMIT_EVENTS:
                dt = b["t"] - a["t"]
                dx = math.hypot(b["x"] - a["x"], b["y"] - a["y"])
                gaps.append((bot, a["t"], dt, dx))

    stand = [g for g in gaps if g[2] >= STAND_MIN_DT and g[3] < STAND_MAX_DISP]

    print("== %s" % path)
    print("exit->commit gaps: %d   standing re-decides: %d (%.0f%%)" % (
        len(gaps), len(stand), 100.0 * len(stand) / max(1, len(gaps))))
    if stand:
        dts = [g[2] for g in stand]
        print("standing dt: median=%.1fs mean=%.1fs max=%.1fs   histogram %s" % (
            statistics.median(dts), statistics.mean(dts), max(dts),
            dict(sorted(collections.Counter(round(d) for d in dts).items()))))

        # view churn + speed inside the standing windows
        revs_per_s, speeds = [], []
        for bot, t0, dt, dx in stand:
            tk = [t for t in ticks[bot] if t0 <= t["t"] <= t0 + dt]
            if len(tk) < 6:
                continue
            deltas = [yaw_delta(tk[i]["yaw"], tk[i + 1]["yaw"])
                      for i in range(len(tk) - 1)]
            revs = sum(1 for i in range(len(deltas) - 1)
                       if deltas[i] * deltas[i + 1] < 0
                       and abs(deltas[i]) > 3 and abs(deltas[i + 1]) > 3)
            revs_per_s.append(revs / dt)
            speeds.append(statistics.mean(
                math.hypot(t["vx"], t["vy"]) for t in tk))
        if revs_per_s:
            print("inside standing windows: yaw reversals/sec=%.1f  mean speed=%.0f" % (
                statistics.mean(revs_per_s), statistics.mean(speeds)))

    # broader stillness context
    slow = {0: [0, 0], 1: [0, 0]}
    for bot, tk in ticks.items():
        for t in tk:
            m = 1 if t.get("mode") == 1 else 0
            slow[m][0] += 1
            slow[m][1] += math.hypot(t["vx"], t["vy"]) < 40
    for m, name in ((0, "EXPLORE"), (1, "GOAL")):
        n, s = slow[m]
        if n:
            print("%s ticks: %d (%.0f%% slow<40ups)" % (name, n, 100.0 * s / n))
    print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for p in sys.argv[1:]:
        analyze(p)
