"""Transplant learned link costs from one .nav graph onto another.

Usage:
    py tools/nav_transplant_costs.py <donor.nav> <recipient.nav> <out.nav>

Copies each donor link's cost onto the matching recipient link, where a match
means: both endpoints' node origins coincide (within a small tolerance) and
the link type is the same.  Nodes/links that exist only in the donor (e.g.
grown during a maturation session) are ignored, so the recipient's topology
is unchanged -- this isolates "learned costs" from the known node-growth
confound (see the ozbot-nav-maturation-finding memory).

Format (see CLAUDE.md): int magic 'ONAV', int version=1, int num_nodes;
per node: 3f origin, B flags, i num_links, num_links * {i to; B type; 3x pad;
f cost}.
"""
import struct
import sys

MAGIC = 0x4F4E4156  # not used for compare; we read and echo whatever is there
TOL = 1.0  # node-origin match tolerance (units)


def read_nav(path):
    with open(path, "rb") as f:
        data = f.read()
    off = 0
    magic, version, num_nodes = struct.unpack_from("<iii", data, off)
    off += 12
    nodes = []
    for _ in range(num_nodes):
        x, y, z = struct.unpack_from("<3f", data, off)
        off += 12
        flags, = struct.unpack_from("<B", data, off)
        off += 1
        num_links, = struct.unpack_from("<i", data, off)
        off += 4
        links = []
        for _ in range(num_links):
            to, ltype = struct.unpack_from("<iB", data, off)
            off += 8  # int to + byte type + 3 pad
            cost, = struct.unpack_from("<f", data, off)
            off += 4
            links.append({"to": to, "type": ltype, "cost": cost})
        nodes.append({"origin": (x, y, z), "flags": flags, "links": links})
    return {"magic": magic, "version": version, "nodes": nodes}


def write_nav(path, nav):
    out = bytearray()
    out += struct.pack("<iii", nav["magic"], nav["version"], len(nav["nodes"]))
    for n in nav["nodes"]:
        out += struct.pack("<3f", *n["origin"])
        out += struct.pack("<B", n["flags"])
        out += struct.pack("<i", len(n["links"]))
        for l in n["links"]:
            out += struct.pack("<iB3x", l["to"], l["type"])
            out += struct.pack("<f", l["cost"])
    with open(path, "wb") as f:
        f.write(bytes(out))


def key(origin):
    return tuple(round(c / TOL) for c in origin)


def main():
    donor_path, recip_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    donor = read_nav(donor_path)
    recip = read_nav(recip_path)

    # map recipient node index by quantized origin
    donor_by_key = {}
    for i, n in enumerate(donor["nodes"]):
        donor_by_key.setdefault(key(n["origin"]), i)

    matched = changed = total = 0
    for n in recip["nodes"]:
        di = donor_by_key.get(key(n["origin"]))
        for l in n["links"]:
            total += 1
            if di is None:
                continue
            to_key = key(recip["nodes"][l["to"]]["origin"])
            dto = donor_by_key.get(to_key)
            if dto is None:
                continue
            for dl in donor["nodes"][di]["links"]:
                if dl["to"] == dto and dl["type"] == l["type"]:
                    matched += 1
                    if abs(dl["cost"] - l["cost"]) > 0.01:
                        changed += 1
                    l["cost"] = dl["cost"]
                    break

    write_nav(out_path, recip)
    print(f"recipient: {len(recip['nodes'])} nodes, {total} links")
    print(f"donor:     {len(donor['nodes'])} nodes")
    print(f"matched {matched}/{total} links; {changed} costs actually changed")


if __name__ == "__main__":
    main()
