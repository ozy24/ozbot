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
    python dm2_combat.py need [mapname] [--limit N]   -> resource-need thresholds
                                                          (all maps if mapname omitted)
    python dm2_combat.py one <demo.dm2>                -> single-demo debug dump

The `need` subcommand mines the human "resource need" curves the bot's goal
scorer wants (bot_goal.c Item_Score): at what health/armor/ammo level do pros
actually detour to pick a thing up.  It writes durable calibration targets to
demos/derived/combat_need/thresholds.json.
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

# ammo type each weapon consumes (canonical weapon names as resolve_weapon emits
# them).  Used to bucket "how much ammo did they have" by the ammo type that
# matters -- absolute counts are only comparable within a type (rockets cap 50,
# cells cap 200).  Blaster has no ammo.
WEAPON_AMMO = {
    "shotgun": "shells", "super shotgun": "shells",
    "machinegun": "bullets", "chaingun": "bullets",
    "grenade launcher": "grenades", "rocket launcher": "rockets",
    "hyperblaster": "cells", "bfg10k": "cells",
    "railgun": "slugs",
}

# substring -> ammo type, for classifying an ammo PICKUP item by its name
AMMO_ITEM_TYPE = [
    ("Shells", "shells"), ("Bullets", "bullets"), ("Cells", "cells"),
    ("Rockets", "rockets"), ("Slugs", "slugs"), ("Grenades", "grenades"),
]


def item_category(name):
    """Coarse item class from its configstring pickup name -- mirrors the
    vocabulary of bot_goal.c Item_BaseValue so the analysis and the bot agree."""
    n = name.lower()
    # ammo first: "Grenades" is ammo, the "Grenade Launcher" weapon is caught below
    for sub, _ in AMMO_ITEM_TYPE:
        if sub.lower() in n and "launcher" not in n:
            return "ammo"
    if "armor" in n or "shard" in n:
        return "armor"
    if any(w in n for w in (
            "shotgun", "machinegun", "chaingun", "launcher", "hyperblaster",
            "railgun", "bfg", "blaster", "grenade launcher")):
        return "weapon"
    if any(p in n for p in (
            "quad", "invulnerability", "silencer", "rebreather", "environment",
            "adrenaline", "bandolier", "pack", "power shield", "power screen")):
        return "powerup"
    if "health" in n or "mega" in n or "stimpack" in n or "medkit" in n:
        return "health"
    return "other"


def ammo_item_type(name):
    """Ammo type of an ammo PICKUP item (or None)."""
    n = name.lower()
    if "launcher" in n:
        return None
    for sub, atype in AMMO_ITEM_TYPE:
        if sub.lower() in n:
            return atype
    return None

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
    # one-frame-behind snapshot: the decision state BEFORE a pickup mutates it
    # (a health/ammo/weapon pickup bumps STAT_HEALTH/STAT_AMMO on the same frame
    # the pickup edge fires, so the pickup-frame value is post-pickup-inflated).
    prev_health = prev_armor = prev_ammo = 0
    prev_weapon = None

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
                        # log the PRE-pickup snapshot (the state that drove the
                        # decision to grab it), not the post-pickup inflated stats
                        info["pickups"].append(
                            (frame_idx, idx, prev_health, prev_armor,
                             prev_ammo, prev_weapon))
                        stats[STAT_PICKUP_STRING] = 0   # one-shot; only log the edge
                    if last_origin is not None and last_viewangles is not None:
                        info["samples"].append(
                            (frame_idx, last_viewangles[0], last_viewangles[1],
                             last_weapon, stats[STAT_HEALTH], stats[STAT_ARMOR],
                             stats[STAT_AMMO]))
                    # snapshot this frame's state as the "before" for the next frame
                    prev_health = stats[STAT_HEALTH]
                    prev_armor = stats[STAT_ARMOR]
                    prev_ammo = stats[STAT_AMMO]
                    prev_weapon = last_weapon
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
            f0, y0, p0, w0, h0, a0, am0 = samp[i - 1]
            f1, y1, p1, w1, h1, a1, am1 = samp[i]
            if f1 != f0 + 1:
                continue    # dropped/duplicated frame -- don't blend the rate
            dy = abs(((y1 - y0) + 180) % 360 - 180)
            dp = abs(p1 - p0)
            if dy + dp > 200:
                continue    # respawn/teleport view snap, not real aim movement
            turn_rates.append(dy + dp)

        for frame_idx, yaw, pitch, weapon_idx, health, armor, ammo in samp:
            w = resolve_weapon(info["models"], weapon_idx)
            if w:
                equip_time[w] += 1

        last_pickup_frame = None
        for frame_idx, idx, health, armor, ammo, weapon_idx in info["pickups"]:
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


def _pctile(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = min(int(len(xs) * p), len(xs) - 1)
    return xs[k]


def _summ(xs):
    return {
        "n": len(xs),
        "p10": _pctile(xs, 0.10), "p25": _pctile(xs, 0.25),
        "p50": _pctile(xs, 0.50), "p75": _pctile(xs, 0.75),
        "p90": _pctile(xs, 0.90),
        "mean": round(sum(xs) / len(xs), 1) if xs else 0.0,
    }


def need(mapname, limit):
    """Mine human resource-need thresholds across the corpus (all maps by
    default): at what health/armor/ammo did pros decide to grab each item
    category.  Writes demos/derived/combat_need/thresholds.json."""
    import datetime

    pickup_state = defaultdict(lambda: defaultdict(list))  # cat -> field -> [vals]
    ammo_by_type = defaultdict(list)   # ammo type -> [pre_ammo] on a matched top-up
    ammo_health = []                   # health at any ammo pickup
    ammo_match = ammo_mismatch = ammo_noweapon = 0
    wsw = {"n": 0, "switched": 0, "delays": []}
    wsw_by = defaultdict(lambda: {"n": 0, "switched": 0, "delays": []})
    equip_time = Counter()
    kill_weapon_counts = Counter()
    maps = Counter()
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
        if info["map"]:
            maps[info["map"]] += 1
        models = info["models"]

        # weapon held per frame (for weapon-switch detection + equip-time share)
        frame_weap = {}
        for s in info["samples"]:
            fw = resolve_weapon(models, s[3])
            if fw:
                frame_weap[s[0]] = fw
                equip_time[fw] += 1

        for frame_idx, idx, pre_h, pre_a, pre_ammo, pre_widx in info["pickups"]:
            item = info["items"].get(idx, "")
            if not item:
                continue
            cat = item_category(item)
            held = resolve_weapon(models, pre_widx)
            if cat == "health":
                pickup_state["health"]["health"].append(pre_h)
                pickup_state["health"]["armor"].append(pre_a)
            elif cat == "armor":
                pickup_state["armor"]["armor"].append(pre_a)
                pickup_state["armor"]["health"].append(pre_h)
            elif cat == "weapon":
                pickup_state["weapon"]["health"].append(pre_h)
                picked = item.lower()      # matches resolve_weapon output
                wsw["n"] += 1
                wsw_by[picked]["n"] += 1
                for d in range(1, 21):     # scan forward up to ~2s (20 frames)
                    if frame_weap.get(frame_idx + d) == picked:
                        wsw["switched"] += 1
                        wsw["delays"].append(d * 100)
                        wsw_by[picked]["switched"] += 1
                        wsw_by[picked]["delays"].append(d * 100)
                        break
            elif cat == "ammo":
                ammo_health.append(pre_h)
                atype = ammo_item_type(item)
                held_ammo = WEAPON_AMMO.get(held) if held else None
                # stat 3 is ammo for the CURRENTLY-HELD weapon, so it's only the
                # right "how low was I" reading when the ammo they grabbed feeds
                # the gun in their hands -- the deliberate top-up we want to model.
                if held_ammo is None:
                    ammo_noweapon += 1
                elif atype and atype == held_ammo:
                    ammo_by_type[atype].append(pre_ammo)
                    ammo_match += 1
                else:
                    ammo_mismatch += 1

        me = info["names"].get(info["playernum"], "").lower()
        for frame_idx, victim, attacker, weapon in info["kills"]:
            if me and attacker.lower() == me:
                kill_weapon_counts[weapon] += 1

    total_eq = sum(equip_time.values()) or 1
    total_kills = sum(kill_weapon_counts.values()) or 1

    out = {
        "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "corpus": {
            "demos_parsed": demos_ok, "demos_skipped": demos_bad,
            "maps": dict(maps.most_common()),
        },
        "pickup_need": {
            "health": {
                "health_at_pickup": _summ(pickup_state["health"]["health"]),
                "armor_at_pickup": _summ(pickup_state["health"]["armor"]),
            },
            "armor": {
                "armor_at_pickup": _summ(pickup_state["armor"]["armor"]),
                "health_at_pickup": _summ(pickup_state["armor"]["health"]),
            },
            "ammo": {
                "health_at_pickup": _summ(ammo_health),
                "matched_pickups": ammo_match,
                "mismatched_pickups": ammo_mismatch,
                "no_owned_weapon_pickups": ammo_noweapon,
                "by_ammo_type": {t: _summ(v) for t, v in sorted(ammo_by_type.items())},
            },
            "weapon": {
                "health_at_pickup": _summ(pickup_state["weapon"]["health"]),
            },
        },
        "weapon_switch": {
            "n_weapon_pickups": wsw["n"],
            "switched_within_2s_pct":
                round(100.0 * wsw["switched"] / wsw["n"], 1) if wsw["n"] else 0.0,
            "switch_delay_ms": {"p50": _pctile(wsw["delays"], 0.5),
                                "p90": _pctile(wsw["delays"], 0.9)},
            "by_weapon": {
                w: {"n": d["n"],
                    "switched_pct":
                        round(100.0 * d["switched"] / d["n"], 1) if d["n"] else 0.0,
                    "delay_ms_p50": _pctile(d["delays"], 0.5)}
                for w, d in sorted(wsw_by.items(), key=lambda kv: -kv[1]["n"])
            },
        },
        "weapon_equipped_pct":
            {w: round(100.0 * c / total_eq, 1) for w, c in equip_time.most_common()},
        "weapon_at_kill_pct":
            {w: round(100.0 * c / total_kills, 1) for w, c in kill_weapon_counts.most_common()},
        "calibration": {
            "bot_healthneed": {
                "urgency_health_p50": _pctile(pickup_state["health"]["health"], 0.5),
                "note": "median health at which pros top up; urgency curve should already pull by here",
            },
            "bot_ammoneed": {
                "low_threshold_by_ammo":
                    {t: _pctile(v, 0.5) for t, v in sorted(ammo_by_type.items())},
                "note": "p50 of ammo-for-held-weapon at the moment they refilled that ammo type",
            },
            "bot_wpnneed": {
                "kill_rank": [w for w, _ in kill_weapon_counts.most_common()],
            },
        },
    }

    outdir = os.path.join(ROOT, "demos", "derived", "combat_need")
    os.makedirs(outdir, exist_ok=True)
    outpath = os.path.join(outdir, "thresholds.json")
    with open(outpath, "w") as f:
        json.dump(out, f, indent=2)

    # ---- console summary ----
    print(f"\n=== {mapname or 'ALL MAPS'}: {demos_ok} demos parsed, {demos_bad} skipped ===")
    print(f"maps: {dict(maps.most_common(8))}{' ...' if len(maps) > 8 else ''}\n")

    def line(tag, s):
        print(f"  {tag:22s} n={s['n']:7d}  p25={s['p25']:5.0f} p50={s['p50']:5.0f} "
              f"p75={s['p75']:5.0f}  mean={s['mean']:5.1f}")

    print("-- HEALTH: player state when they picked up a health item --")
    line("health-at-pickup", _summ(pickup_state["health"]["health"]))
    line("armor-at-pickup", _summ(pickup_state["health"]["armor"]))
    print("\n-- ARMOR: player state when they picked up an armor item --")
    line("armor-at-pickup", _summ(pickup_state["armor"]["armor"]))
    line("health-at-pickup", _summ(pickup_state["armor"]["health"]))
    print("\n-- AMMO: ammo-for-held-weapon when refilling THAT ammo type --")
    print(f"  (matched top-ups={ammo_match}  mismatched={ammo_mismatch}  "
          f"no-owned-weapon={ammo_noweapon})")
    for t, v in sorted(ammo_by_type.items()):
        line(t, _summ(v))
    line("health-at-ammo-pickup", _summ(ammo_health))
    print("\n-- WEAPON: switch to a freshly-grabbed gun within 2s --")
    print(f"  {out['weapon_switch']['switched_within_2s_pct']:.1f}% of "
          f"{wsw['n']} weapon pickups; delay p50="
          f"{out['weapon_switch']['switch_delay_ms']['p50']}ms")
    print("  weapon-at-kill %: " + ", ".join(
        f"{w} {p}" for w, p in list(out["weapon_at_kill_pct"].items())[:8]))
    print(f"\n-> wrote {outpath}")
    return out


def one(path):
    data = open(path, "rb").read()
    info = parse_combat(data)
    print(f"map={info['map']} player={info['names'].get(info['playernum'])}")
    print(f"samples={len(info['samples'])} pickups={len(info['pickups'])} "
          f"kills={len(info['kills'])}")
    print("items seen:", {k: v for k, v in list(info["items"].items())[:10]})
    for p in info["pickups"][:10]:
        idx = p[1]
        w = resolve_weapon(info["models"], p[5])
        print("pickup:", info["items"].get(idx, f"#{idx}"),
              "pre_health=", p[2], "pre_armor=", p[3], "pre_ammo=", p[4],
              "holding=", w)
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
        if cmd == "need":
            result = need(mapname, limit)
        else:
            result = scan(mapname, limit)
