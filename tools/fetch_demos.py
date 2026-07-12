#!/usr/bin/env python3
"""
Download Quake 2 demo archives from demos.q2players.org into a folder.

Scope (task 1): enumerate the listing pages and download each .zip archive,
as-is, into demos/raw/.  No extraction/sorting here.  Resumable and polite.

The site's TLS cert is expired, so verification is disabled for this host.

Usage:
    python fetch_demos.py enumerate          # crawl listings -> demos/urls.txt
    python fetch_demos.py download [--limit N]   # fetch missing archives
    python fetch_demos.py status
"""

import os
import re
import ssl
import sys
import time
import urllib.request

BASE = "https://demos.q2players.org"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "demos")
RAW = os.path.join(OUT, "raw")
URLS = os.path.join(OUT, "urls.txt")
LOG = os.path.join(OUT, "download.log")

UA = {"User-Agent": "Mozilla/5.0 (ozbot demo archive fetcher; personal research)"}
DELAY = 0.4        # seconds between requests (be kind to a volunteer server)
TIMEOUT = 60
RETRIES = 3

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


def log(msg):
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    os.makedirs(OUT, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, context=_ctx, timeout=TIMEOUT)


def fetch_retry(url, binary=False):
    for attempt in range(RETRIES):
        try:
            data = fetch(url).read()
            return data if binary else data.decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            if attempt == RETRIES - 1:
                raise
            time.sleep(1 + attempt * 2)
    return None


def enumerate_demos():
    os.makedirs(OUT, exist_ok=True)
    order, seen = [], set()
    frm = 0
    while True:
        url = f"{BASE}/demos?from={frm}"
        try:
            html = fetch_retry(url)
        except Exception as e:  # noqa: BLE001
            log(f"enum ERR {url}: {e}")
            break
        links = re.findall(r'(/dl/\d+/[^"\']+?\.zip)(?!\?)', html)
        for l in links:
            if l not in seen:
                seen.add(l)
                order.append(l)
        m = re.search(r'([\d,]+) results found', html)
        total = m.group(1) if m else "?"
        log(f"enum from={frm} links={len(links)} collected={len(order)} reported={total}")
        if not links or "Next Page" not in html:
            break
        frm += 40
        time.sleep(DELAY)
    with open(URLS, "w", encoding="utf-8") as f:
        f.write("\n".join(order) + "\n")
    log(f"enumerate done: {len(order)} urls -> {URLS}")


def dest_name(path):
    # path = /dl/<id>/<name>.zip  ->  "<id>_<sanitized name>"
    parts = path.strip("/").split("/")
    did = parts[1] if len(parts) > 1 else "0"
    name = parts[-1]
    safe = re.sub(r'[^A-Za-z0-9._-]', '_', name)
    return f"{did}_{safe}"


def download_all(limit=None):
    os.makedirs(RAW, exist_ok=True)
    urls = [l.strip() for l in open(URLS, encoding="utf-8") if l.strip()]
    new = skipped = failed = 0
    for path in urls:
        dest = os.path.join(RAW, dest_name(path))
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            skipped += 1
            continue
        try:
            data = fetch_retry(BASE + path, binary=True)
            if not data or len(data) < 64:
                raise IOError(f"suspiciously small ({0 if not data else len(data)} bytes)")
            tmp = dest + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, dest)
            new += 1
            if new % 25 == 0:
                log(f"progress: new={new} skipped={skipped} failed={failed} "
                    f"last={os.path.basename(dest)}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            log(f"FAIL {path}: {e}")
        time.sleep(DELAY)
        if limit and new >= limit:
            break
    log(f"download pass done: new={new} skipped={skipped} failed={failed} "
        f"urls={len(urls)}")


def status():
    n = len([f for f in os.listdir(RAW) if f.endswith(".zip")]) if os.path.isdir(RAW) else 0
    u = sum(1 for _ in open(URLS)) if os.path.exists(URLS) else 0
    size = 0
    if os.path.isdir(RAW):
        size = sum(os.path.getsize(os.path.join(RAW, f)) for f in os.listdir(RAW))
    print(f"urls listed: {u}")
    print(f"archives downloaded: {n}")
    print(f"raw size: {size/1e6:.1f} MB")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "enumerate":
        enumerate_demos()
    elif cmd == "download":
        lim = None
        if "--limit" in sys.argv:
            lim = int(sys.argv[sys.argv.index("--limit") + 1])
        download_all(lim)
    else:
        status()
