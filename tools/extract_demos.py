#!/usr/bin/env python3
"""
Extract .dm2 demos from demos/raw/*.zip into demos/sorted/<map>/ with a JSONL manifest.

Never modifies demos/raw/.  Resumable: skips ids already in manifest with an existing
sorted file on disk.

Usage:
    python extract_demos.py seed-sample [--zip PATH]
    python extract_demos.py extract [--map MAP] [--limit N]
    python extract_demos.py status
"""

import glob
import json
import os
import re
import sys
import zipfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dm2parse

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMOS = os.path.join(REPO, "demos")
RAW = os.path.join(DEMOS, "raw")
SORTED = os.path.join(DEMOS, "sorted")
WORK = os.path.join(DEMOS, "work")
MANIFEST = os.path.join(DEMOS, "manifest.jsonl")
SAMPLE = os.path.join(WORK, "sample_q2dm1.dm2")

MAP_SUFFIX = re.compile(
    r"_((?:q2|ztn|kold|blood|ptrip|frdm|arena)[a-zA-Z0-9]*)$", re.I)
SERIES_MAP = re.compile(r"_(map\d+of\d+)_([^_]+)$", re.I)
LEAGUE = re.compile(r"^((?:EDL|ADL)\d+|2x2\d+)", re.I)
RAW_NAME = re.compile(r"^(\d+)_(.+)\.zip$", re.I)
DATE = re.compile(r"(\d{8})")
UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_token(s):
    """Filesystem-safe player token; keep alnum, dot, hyphen, underscore."""
    s = UNSAFE.sub("", s)
    return re.sub(r"[^A-Za-z0-9._-]", "_", s) or "unknown"


def parse_raw_zip(basename):
    """
    Parse metadata from a local raw zip filename:
      5801_EDL18_Division_3b_20191024_APKIS-vs-dolarp_map2of2_q2dm1.zip
    Returns dict with id, archive, map, date, date_valid, players, league, stage, series, parse_ok.
    """
    m = RAW_NAME.match(basename)
    if not m:
        return {"id": 0, "archive": basename, "parse_ok": False}

    did = int(m.group(1))
    archive = m.group(2)
    stem = archive
    warnings = []

    series = None
    map_name = None
    stem_before_map = stem

    sm = SERIES_MAP.search(stem)
    if sm:
        series = sm.group(1)
        map_name = sm.group(2)
        stem_before_map = stem[: sm.start()]
    else:
        mm = MAP_SUFFIX.search(stem)
        if mm:
            map_name = mm.group(1)
            stem_before_map = stem[: mm.start()]
        else:
            idx = stem.rfind("_")
            if idx >= 0:
                map_name = stem[idx + 1 :]
                stem_before_map = stem[:idx]
            else:
                map_name = "unknown"
                warnings.append("map_not_found_in_name")

    league = stage = None
    date = None
    date_valid = False
    p1, p2 = "unknown", "unknown"

    dm = DATE.search(stem)
    if dm:
        date = dm.group(1)
        date_valid = date != "00000000"
        if date in stem_before_map:
            players_part = stem_before_map[stem_before_map.find(date) + 8 :].lstrip("_")
            if "-vs-" in players_part:
                p1, p2 = players_part.split("-vs-", 1)
                p2 = p2.rstrip("_")
            else:
                warnings.append("players_not_found")
        else:
            warnings.append("players_not_found")
    else:
        warnings.append("date_not_found")

    lm = LEAGUE.match(stem)
    if lm:
        league = lm.group(1)
        rest = stem[lm.end() :].lstrip("_")
        if dm:
            stage = rest[: rest.find(dm.group(1))].rstrip("_") or None
    else:
        warnings.append("league_not_found")

    parse_ok = "-vs-" in stem and map_name and map_name != "unknown"
    p1, p2 = safe_token(p1), safe_token(p2)

    return {
        "id": did,
        "archive": archive,
        "map": map_name,
        "date": date,
        "date_valid": date_valid,
        "players": [p1, p2],
        "league": league,
        "stage": stage,
        "series": series,
        "parse_ok": parse_ok,
        "warnings": warnings,
    }


def sorted_basename(meta):
    """{date|undated}_{p1}-vs-{p2}_{id}.dm2"""
    prefix = meta["date"] if meta.get("date_valid") else "undated"
    p1, p2 = meta["players"]
    return f"{prefix}_{p1}-vs-{p2}_{meta['id']}.dm2"



def load_manifest_index():
    """Return {id: sorted_rel} for manifest rows whose sorted file exists."""
    index = {}
    if not os.path.isfile(MANIFEST):
        return index
    with open(MANIFEST, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                did = int(row["id"])
                rel = row.get("sorted") or ""
                if rel and os.path.isfile(os.path.join(DEMOS, rel)):
                    index[did] = rel
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
    return index


def manifest_row_exists(did, sorted_rel, index=None):
    """True if manifest has this id and the sorted file exists."""
    if index is not None:
        return did in index
    if not os.path.isfile(MANIFEST):
        return False
    with open(MANIFEST, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if int(row.get("id", -1)) != did:
                    continue
                rel = row.get("sorted", "")
                if rel and os.path.isfile(os.path.join(DEMOS, rel)):
                    return True
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
    return False


def append_manifest(row):
    os.makedirs(DEMOS, exist_ok=True)
    with open(MANIFEST, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_dm2_from_zip(zip_path):
    """Return (inner_name, bytes) for the first .dm2 member, or (None, None)."""
    with zipfile.ZipFile(zip_path) as z:
        inner = next((n for n in z.namelist() if n.lower().endswith(".dm2")), None)
        if not inner:
            return None, None
        return inner, z.read(inner)


def extract_one(zip_path, *, dry_run=False, done_index=None):
    """
    Extract one raw zip.  Returns manifest dict on success, None on skip/fail.
    """
    basename = os.path.basename(zip_path)
    meta = parse_raw_zip(basename)
    did = meta["id"]

    if manifest_row_exists(did, None, done_index):
        return None

    inner, data = read_dm2_from_zip(zip_path)
    if not data:
        row = _failure_row(basename, meta, "no_dm2_in_zip")
        if not dry_run:
            append_manifest(row)
        return row

    parsed = dm2parse.parse_data(data)
    parsed_map = parsed.get("map") or meta.get("map") or "unknown"
    map_folder = parsed_map

    warnings = list(meta.get("warnings") or [])
    if meta.get("map") and parsed_map != meta["map"]:
        warnings.append(f"map_mismatch:name={meta['map']},parsed={parsed_map}")

    fname = sorted_basename(meta)
    sorted_rel = f"sorted/{map_folder}/{fname}"
    sorted_abs = os.path.join(DEMOS, sorted_rel)

    if not dry_run:
        os.makedirs(os.path.dirname(sorted_abs), exist_ok=True)
        with open(sorted_abs, "wb") as f:
            f.write(data)

    parsed_names = {str(k): v for k, v in parsed.get("names", {}).items()}
    row = {
        "id": did,
        "sorted": sorted_rel.replace("\\", "/"),
        "raw_zip": f"raw/{basename}".replace("\\", "/"),
        "archive": meta["archive"],
        "map": map_folder,
        "date": meta.get("date"),
        "date_valid": meta.get("date_valid", False),
        "players": meta["players"],
        "series": meta.get("series"),
        "league": meta.get("league"),
        "stage": meta.get("stage"),
        "inner_dm2": inner,
        "parsed_map": parsed_map,
        "parsed_names": parsed_names,
        "playernum": parsed.get("playernum"),
        "frames": len(parsed.get("frames") or []),
        "parse_ok": meta.get("parse_ok", False),
        "warnings": warnings,
        "extracted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if not dry_run:
        append_manifest(row)
    return row


def _failure_row(basename, meta, reason):
    return {
        "id": meta.get("id", 0),
        "sorted": None,
        "raw_zip": f"raw/{basename}".replace("\\", "/"),
        "archive": meta.get("archive", basename),
        "map": meta.get("map"),
        "date": meta.get("date"),
        "date_valid": meta.get("date_valid", False),
        "players": meta.get("players", ["unknown", "unknown"]),
        "series": meta.get("series"),
        "league": meta.get("league"),
        "stage": meta.get("stage"),
        "inner_dm2": None,
        "parsed_map": None,
        "parsed_names": {},
        "playernum": None,
        "frames": 0,
        "parse_ok": False,
        "warnings": (meta.get("warnings") or []) + [reason],
        "extracted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def find_q2dm1_zip():
    """Pick first q2dm1 zip in raw/ for seed-sample."""
    pattern = os.path.join(RAW, "*q2dm1*.zip")
    files = sorted(glob.glob(pattern))
    files = [
        f
        for f in files
        if re.search(r"(?<![A-Za-z0-9])q2dm1(?![0-9])", os.path.basename(f))
    ]
    return files[0] if files else None


def seed_sample(zip_path=None):
    zip_path = zip_path or find_q2dm1_zip()
    if not zip_path or not os.path.isfile(zip_path):
        print("no q2dm1 zip found in demos/raw/", file=sys.stderr)
        return 1
    inner, data = read_dm2_from_zip(zip_path)
    if not data:
        print(f"no .dm2 in {zip_path}", file=sys.stderr)
        return 1
    os.makedirs(WORK, exist_ok=True)
    with open(SAMPLE, "wb") as f:
        f.write(data)
    info = dm2parse.parse_data(data)
    print(f"seeded {SAMPLE}")
    print(f"  from {zip_path} ({inner})")
    print(f"  map={info['map']} player={info['names'].get(info['playernum'])} "
          f"frames={len(info['frames'])}")
    return 0


def extract_all(map_filter=None, limit=None):
    zips = sorted(glob.glob(os.path.join(RAW, "*.zip")))
    if map_filter:
        zips = [
            z
            for z in zips
            if re.search(
                rf"(?<![A-Za-z0-9]){re.escape(map_filter)}(?![0-9])",
                os.path.basename(z),
            )
        ]
    done_index = load_manifest_index()
    done = skipped = failed = 0
    for zp in zips:
        meta = parse_raw_zip(os.path.basename(zp))
        if meta["id"] in done_index:
            skipped += 1
            continue
        try:
            row = extract_one(zp, done_index=done_index)
            if row is None:
                skipped += 1
            elif row.get("sorted"):
                done += 1
                done_index[meta["id"]] = row["sorted"]
                if done % 10 == 0:
                    print(f"  extracted {done} ...", flush=True)
            else:
                failed += 1
        except (zipfile.BadZipFile, OSError) as e:
            failed += 1
            print(f"FAIL {os.path.basename(zp)}: {e}", file=sys.stderr)
        if limit and done >= limit:
            break
    print(f"extract done: new={done} skipped={skipped} failed={failed} "
          f"scanned={len(zips)}")
    return 0


def status():
    raw_n = len(glob.glob(os.path.join(RAW, "*.zip"))) if os.path.isdir(RAW) else 0
    sorted_n = len(glob.glob(os.path.join(SORTED, "**", "*.dm2"), recursive=True))
    manifest_n = 0
    if os.path.isfile(MANIFEST):
        with open(MANIFEST, encoding="utf-8") as f:
            manifest_n = sum(1 for line in f if line.strip())
    sample = "yes" if os.path.isfile(SAMPLE) else "no"
    print(f"raw zips:      {raw_n}")
    print(f"sorted dm2:    {sorted_n}")
    print(f"manifest rows: {manifest_n}")
    print(f"sample:        {sample} ({SAMPLE})")
    return 0


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "status"

    if cmd == "seed-sample":
        zp = None
        if "--zip" in args:
            zp = args[args.index("--zip") + 1]
        return seed_sample(zp)

    if cmd == "extract":
        map_filter = None
        limit = None
        if "--map" in args:
            map_filter = args[args.index("--map") + 1]
        if "--limit" in args:
            limit = int(args[args.index("--limit") + 1])
        return extract_all(map_filter, limit)

    if cmd == "status":
        return status()

    print(__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
