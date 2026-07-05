#!/usr/bin/env python3
"""
Extract combat/timing signal from pro .dm2 demos -- aim turn-rate, weapon
usage at kill time, and item-pickup patterns (what/when/at-what-health) --
to calibrate bot_combat.c / bot_goal.c parameters against real player
behavior. Movement/nav data from demos was already tried and found not to
transfer (see the ozbot-demo-import-finding memory: pro strafe-jumps/momentum
the bot's simple movement can't reproduce); this deliberately stays
movement-agnostic per that finding's own recommendation.

Reads demos straight out of the zip archives in demos/raw/ (never extracts or
modifies that directory -- same pattern as demo_to_nav.py / demo_coverage.py).

Usage:
    python dm2_combat.py scan [mapname] [--limit N]   -> aggregate stats
    python dm2_combat.py one <demo.dm2>                -> single-demo debug dump
"""

import glob
import json
import math
import os
import re
import struct
import sys
import zipfile
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dm2parse
from dm2parse import Reader, iter_blocks

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RAW = os.path.join(ROOT, "demos", "raw")

# svc ids (protocol 34) not already needed by dm2parse
SVC_PRINT = 10
SVC_CONFIGSTRING = 13
SVC_SPAWNBASELINE = 14
SVC_FRAME = 20
SVC_PLAYERINFO = 17
SVC_SERVERDATA = 12
SVC_STUFFTEXT, SVC_CENTERPRINT, SVC_LAYOUT = 11, 15, 4
SVC_INVENTORY = 5
SVC_DOWNLOAD = 16
SVC_DISCONNECT, SVC_RECONNECT = 7, 8
SVC_NOP = 6

PS_M_TYPE, PS_M_ORIGIN, PS_M_VELOCITY = 1, 2, 4
PS_M_TIME, PS_M_FLAGS, PS_M_GRAVITY, PS_M_DELTA_ANGLES = 8, 16, 32, 64
PS_VIEWOFFSET, PS_VIEWANGLES, PS_KICKANGLES = 128, 256, 512
PS_BLEND, PS_FOV, PS_WEAPONINDEX, PS_WEAPONFRAME, PS_RDFLAGS = 1024, 2048, 4096, 8192, 16384

STAT_HEALTH, STAT_AMMO, STAT_ARMOR = 1, 3, 5
STAT_PICKUP_ICON, STAT_PICKUP_STRING, STAT_SELECTED_ITEM, STAT_FRAGS = 7, 8, 12, 14

CS_ITEMS = 1056     # = CS_LIGHTS + MAX_LIGHTSTYLES, per dm2parse's CS_PLAYERSKINS comment
CS_MODELS = 32
CS_PLAYERSKINS = 1312
MAX_CLIENTS = 256

VIEWMODEL_WEAPON = {
    "v_blast": "blaster", "v_shotg": "shotgun", "v_shotg2": "super shotgun",
    "v_machn": "machinegun", "v_chain": "chaingun", "v_handgr": "grenade launcher",
    "v_launch": "grenade launcher", "v_rocket": "rocket launcher",
    "v_hyperb": "hyperblaster", "v_rail": "railgun", "v_bfg": "bfg10k",
}

# obituary vocabulary, from p_client.c's Obituary() -- "%s %s %s%s\n".
# Matched by substring search (see parse_obituary), not a generic regex --
# a loose regex over free-form print text false-positives constantly (matched
# HUD/ammo-warning strings in testing). message2 (weapon suffix) disambiguates
# a few shared messages; "feels ...'s pain" is a rare/unclear MOD, left "?".
OBIT_WEAPON = {
    "was blasted by": "blaster", "was gunned down by": "machinegun",
    "was blown away by": "super shotgun", "was machinegunned by": "machinegun",
    "was cut in half by": "chaingun", "was popped by": "grenade launcher",
    "was shredded by": "grenade launcher", "ate": "rocket launcher",
    "almost dodged": "rocket launcher", "was melted by": "hyperblaster",
    "was railed by": "railgun", "saw the pretty lights from": "bfg10k",
    "was disintegrated by": "bfg10k", "couldn't hide from": "bfg10k",
    "caught": "grenade launcher", "didn't see": "grenade launcher",
    "feels": "?",
    "tried to invade": "telefrag",
}
_OBIT_MSGS = sorted(OBIT_WEAPON.keys(), key=len, reverse=True)   # longest first


def parse_obituary(text):
    """Returns (victim, attacker, weapon) or None. text is the raw print line.
    Kill lines are "%s %s %s%s\n" (victim, msg, attacker, weapon-suffix) --
    NO trailing period (only the separate self-kill format has one)."""
    body = text.rstrip("\n")
    for msg in _OBIT_MSGS:
        marker = " " + msg + " "
        idx = body.find(marker)
        if idx <= 0:
            continue
        victim = body[:idx]
        rest = body[idx + len(marker):]
        apos = rest.find("'s ")
        attacker = rest[:apos] if apos >= 0 else rest
        if not victim or not attacker or " " in attacker or " " in victim:
            continue   # names are single tokens in this demo set; guards false positives
        return victim, attacker, OBIT_WEAPON[msg]
    return None


def angle16(v):
    return v * (360.0 / 65536.0)


def read_playerinfo(r, stats):
    """Decodes what dm2parse skips: viewangles, weaponindex, stats deltas.
    Returns (origin_or_None, viewangles_or_None, weaponindex_or_None)."""
    flags = r.u16()
    origin = viewangles = weaponindex = None
    if flags & PS_M_TYPE:
        r.skip(1)
    if flags & PS_M_ORIGIN:
        origin = (r.s16(), r.s16(), r.s16())
    if flags & PS_M_VELOCITY:
        r.skip(6)
    if flags & PS_M_TIME:
        r.skip(1)
    if flags & PS_M_FLAGS:
        r.skip(1)
    if flags & PS_M_GRAVITY:
        r.skip(2)
    if flags & PS_M_DELTA_ANGLES:
        r.skip(6)
    if flags & PS_VIEWOFFSET:
        r.skip(3)
    if flags & PS_VIEWANGLES:
        viewangles = (angle16(r.s16()), angle16(r.s16()), angle16(r.s16()))
    if flags & PS_KICKANGLES:
        r.skip(3)
    if flags & PS_WEAPONINDEX:
        weaponindex = r.u8()
    if flags & PS_WEAPONFRAME:
        r.skip(7)
    if flags & PS_BLEND:
        r.skip(4)
    if flags & PS_FOV:
        r.skip(1)
    if flags & PS_RDFLAGS:
        r.skip(1)
    statbits = r.u32()
    for i in range(32):
        if statbits & (1 << i):
            stats[i] = r.s16()
    return origin, viewangles, weaponindex


def parse_combat(data):
    info = {"map": None, "playernum": None, "names": {}, "items": {}, "models": {},
            "samples": [], "pickups": [], "kills": []}
    m = re.search(rb"maps/([A-Za-z0-9_]+)\.bsp", data)
    if m:
        info["map"] = m.group(1).decode()

    stats = [0] * 32
    last_origin = None
    last_viewangles = None
    last_weapon = None
    frame_idx = 0

    for block in iter_blocks(data):
        if not block:
            continue
        r = Reader(block)
        while not r.eob():
            cmd = r.u8()
            if cmd == SVC_SERVERDATA:
                r.s32(); r.s32(); r.u8()
                r.string()                       # gamedir
                info["playernum"] = r.s16()
                r.string()                       # levelname
            elif cmd == SVC_CONFIGSTRING:
                idx = r.u16()
                s = r.string()
                if CS_PLAYERSKINS <= idx < CS_PLAYERSKINS + MAX_CLIENTS:
                    info["names"][idx - CS_PLAYERSKINS] = s.split("\\")[0]
                elif CS_ITEMS <= idx < CS_ITEMS + 256:
                    info["items"][idx - CS_ITEMS] = s
                elif CS_MODELS <= idx < CS_ITEMS:
                    info["models"][idx - CS_MODELS] = s
            elif cmd == SVC_PRINT:
                r.u8()
                text = r.string()
                obit = parse_obituary(text)
                if obit:
                    victim, attacker, weapon = obit
                    info["kills"].append((frame_idx, victim, attacker, weapon))
            elif cmd in (SVC_STUFFTEXT, SVC_CENTERPRINT, SVC_LAYOUT):
                r.string()
            elif cmd == SVC_INVENTORY:
                r.skip(2 * 256)
            elif cmd == SVC_NOP:
                continue
            elif cmd == SVC_DOWNLOAD:
                size = r.s16()
                if size >= 0:
                    r.u8(); r.skip(size)
            elif cmd == SVC_FRAME:
                r.s32(); r.s32(); r.u8()
                arealen = r.u8()
                r.skip(arealen)
                sub = r.u8()
                if sub == SVC_PLAYERINFO:
                    o, va, wi = read_playerinfo(r, stats)
                    if o is not None:
                        last_origin = o
                    if va is not None:
                        last_viewangles = va
                    if wi is not None:
                        last_weapon = wi
                    if stats[STAT_PICKUP_STRING]:
                        idx = stats[STAT_PICKUP_STRING] - CS_ITEMS
                        info["pickups"].append(
                            (frame_idx, idx, stats[STAT_HEALTH], stats[STAT_ARMOR]))
                        stats[STAT_PICKUP_STRING] = 0   # one-shot; only log the edge
                    if last_origin is not None and last_viewangles is not None:
                        info["samples"].append(
                            (frame_idx, last_viewangles[0], last_viewangles[1],
                             last_weapon, stats[STAT_HEALTH], stats[STAT_ARMOR]))
                    frame_idx += 1
                break
            elif cmd in (SVC_DISCONNECT, SVC_RECONNECT):
                break
            else:
                break
    return info


def iter_zip_demos(mapname=None, limit=None):
    pattern = f"*{mapname}*.zip" if mapname else "*.zip"
    files = sorted(glob.glob(os.path.join(RAW, pattern)))
    if mapname:
        # filename globbing is substring-based; keep only exact map token matches
        # (e.g. "q2dm1" must not also match "q2dm10"-style or "ztn2dm1")
        files = [f for f in files
                 if re.search(rf'(?<![A-Za-z0-9]){re.escape(mapname)}(?![0-9])', os.path.basename(f))]
    if limit:
        files = files[:limit]
    n = 0
    for zp in files:
        try:
            z = zipfile.ZipFile(zp)
            dm2 = next((x for x in z.namelist() if x.lower().endswith(".dm2")), None)
            if not dm2:
                continue
            data = z.read(dm2)
        except (zipfile.BadZipFile, OSError):
            continue
        n += 1
        yield os.path.basename(zp), data
    print(f"# {n} demo(s) matched", file=sys.stderr)


def resolve_weapon(models, model_idx):
    if model_idx is None:
        return None
    path = models.get(model_idx, "")
    m = re.search(r"v_[a-z0-9]+", path)
    return VIEWMODEL_WEAPON.get(m.group(0)) if m else None


def scan(mapname, limit):
    turn_rates = []          # deg per 100ms tick (both yaw+pitch component-wise)
    equip_time = Counter()   # weapon -> sample count (proxy for time-equipped)
    kill_weapon_counts = Counter()
    pickup_item_counts = Counter()
    pickup_health = defaultdict(list)   # item name -> [health at pickup]
    pickup_gap = []           # seconds since previous pickup (this player)
    demos_ok = demos_bad = 0

    for name, data in iter_zip_demos(mapname, limit):
        try:
            info = parse_combat(data)
        except Exception:
            demos_bad += 1
            continue
        if not info["samples"]:
            demos_bad += 1
            continue
        demos_ok += 1

        samp = info["samples"]
        for i in range(1, len(samp)):
            f0, y0, p0, w0, h0, a0 = samp[i - 1]
            f1, y1, p1, w1, h1, a1 = samp[i]
            if f1 != f0 + 1:
                continue    # dropped/duplicated frame -- don't blend the rate
            dy = abs(((y1 - y0) + 180) % 360 - 180)
            dp = abs(p1 - p0)
            if dy + dp > 200:
                continue    # respawn/teleport view snap, not real aim movement
            turn_rates.append(dy + dp)

        for frame_idx, yaw, pitch, weapon_idx, health, armor in samp:
            w = resolve_weapon(info["models"], weapon_idx)
            if w:
                equip_time[w] += 1

        last_pickup_frame = None
        for frame_idx, idx, health, armor in info["pickups"]:
            item = info["items"].get(idx, f"item#{idx}")
            pickup_item_counts[item] += 1
            pickup_health[item].append(health)
            if last_pickup_frame is not None:
                pickup_gap.append((frame_idx - last_pickup_frame) / 10.0)
            last_pickup_frame = frame_idx

        me = info["names"].get(info["playernum"], "").lower()
        for frame_idx, victim, attacker, weapon in info["kills"]:
            if me and attacker.lower() == me:
                kill_weapon_counts[weapon] += 1

    def pct(xs, p):
        if not xs:
            return 0.0
        xs = sorted(xs)
        k = int(len(xs) * p)
        k = min(k, len(xs) - 1)
        return xs[k]

    print(f"\n=== {mapname or 'ALL MAPS'}: {demos_ok} demos parsed, {demos_bad} skipped ===\n")

    print("-- aim turn rate (deg per 100ms tick, yaw+pitch combined) --")
    print(f"  n={len(turn_rates)}  median={pct(turn_rates,0.50):.1f}  "
          f"p75={pct(turn_rates,0.75):.1f}  p90={pct(turn_rates,0.90):.1f}  "
          f"p99={pct(turn_rates,0.99):.1f}  max={max(turn_rates) if turn_rates else 0:.1f}")
    print(f"  (as deg/sec: median={pct(turn_rates,0.50)*10:.0f}  p90={pct(turn_rates,0.90)*10:.0f}  "
          f"p99={pct(turn_rates,0.99)*10:.0f})")
    print(f"  current bot_combat.c turnstep range: 20-60 deg/tick (200-600 deg/sec) applied EVERY tick while engaged")

    print("\n-- weapon-equipped time (sample count while that weapon was out; a proxy for preference under real availability) --")
    total_eq = sum(equip_time.values())
    for w, c in equip_time.most_common():
        print(f"  {w:20s} {c:8d}  {100.0*c/total_eq:5.1f}%")

    print("\n-- weapon usage at (self) kill time --")
    total_kills = sum(kill_weapon_counts.values())
    for w, c in kill_weapon_counts.most_common():
        pct_ = 100.0 * c / total_kills if total_kills else 0
        print(f"  {w:20s} {c:6d}  {pct_:5.1f}%")

    print("\n-- item pickups (top 20 by frequency) --")
    for item, c in pickup_item_counts.most_common(20):
        hs = pickup_health[item]
        avg_h = sum(hs) / len(hs) if hs else 0
        print(f"  {item:20s} n={c:6d}  avg_health_at_pickup={avg_h:5.0f}")

    print(f"\n-- time between consecutive pickups (this player) --")
    print(f"  n={len(pickup_gap)}  median={pct(pickup_gap,0.5):.1f}s  p25={pct(pickup_gap,0.25):.1f}s")

    return {
        "turn_rates": turn_rates, "equip_time": dict(equip_time),
        "kill_weapon_counts": dict(kill_weapon_counts),
        "pickup_item_counts": dict(pickup_item_counts),
        "pickup_health": {k: v for k, v in pickup_health.items()},
    }


def one(path):
    data = open(path, "rb").read()
    info = parse_combat(data)
    print(f"map={info['map']} player={info['names'].get(info['playernum'])}")
    print(f"samples={len(info['samples'])} pickups={len(info['pickups'])} "
          f"kills={len(info['kills'])}")
    print("items seen:", {k: v for k, v in list(info["items"].items())[:10]})
    for p in info["pickups"][:10]:
        idx = p[1]
        print("pickup:", info["items"].get(idx, f"#{idx}"), "health=", p[2], "armor=", p[3])
    for k in info["kills"][:10]:
        print("kill:", k)
    weapons = Counter()
    for s in info["samples"]:
        w = resolve_weapon(info["models"], s[3])
        if w:
            weapons[w] += 1
    print("weapon-equipped samples:", weapons.most_common())


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if cmd == "one":
        one(sys.argv[2])
    else:
        mapname = None
        limit = None
        rest = sys.argv[2:]
        args = [a for a in rest if not a.startswith("--limit")]
        if args:
            mapname = args[0]
        for a in rest:
            if a.startswith("--limit"):
                limit = int(a.split("=", 1)[1]) if "=" in a else int(rest[rest.index(a) + 1])
        result = scan(mapname, limit)
