# --- build_neural_map.py (v1.0) ---
# Turns the Awen Grid knowledge graph into a 3D layout the VR room can walk
# through. Merges graphify chunk files (and optionally Obsidian [[wikilinks]]),
# runs a force-directed layout in 3D, and caches the result to
# docs/neural_map.json so the headset never waits on a solver.
#
#   py -3.11 build_neural_map.py                    -> research graph
#   py -3.11 build_neural_map.py --include-wikilinks -> + Obsidian vault links
#   py -3.11 build_neural_map.py --iterations 600    -> tighter layout
#
# Layout runs once here rather than in the browser: 1,150 nodes of live physics
# would eat frame budget that VR cannot spare.

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
MEMORY = ROOT / "Memory"
GRAPHIFY = MEMORY / "research md" / "graphify-out"
OUT = ROOT / "docs" / "neural_map.json"

RE_WIKILINK = re.compile(r"\[\[([^\]|#]+)")


def load_graphify() -> tuple[dict, list]:
    nodes, edges = {}, []
    files = sorted(GRAPHIFY.rglob("*.json"))
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for n in d.get("nodes") or []:
            nid = n.get("id")
            if nid and nid not in nodes:
                nodes[nid] = {
                    "id": nid,
                    "label": str(n.get("label") or nid)[:70],
                    "type": n.get("file_type") or "node",
                    "source": n.get("source_file") or "",
                }
        for e in d.get("edges") or []:
            s, t = e.get("source"), e.get("target")
            if s and t and s != t:
                edges.append((s, t, str(e.get("label") or e.get("type") or "")[:40]))
    print(f"  graphify: {len(files)} chunks -> {len(nodes)} nodes, {len(edges)} edges")
    return nodes, edges


def load_wikilinks(nodes: dict, edges: list) -> None:
    """Adds Obsidian [[links]] as an additional edge layer, keyed on note title."""
    by_title = {}
    md_files = [p for p in MEMORY.rglob("*.md") if ".obsidian" not in p.parts]
    for p in md_files:
        by_title.setdefault(p.stem.lower(), p)
    added_nodes = added_edges = 0
    for p in md_files:
        src = f"note::{p.stem.lower()}"
        if src not in nodes:
            nodes[src] = {"id": src, "label": p.stem[:70], "type": "note",
                          "source": str(p.relative_to(MEMORY))}
            added_nodes += 1
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for m in RE_WIKILINK.findall(text):
            tgt_title = m.strip().lower()
            tgt = f"note::{tgt_title}"
            if tgt not in nodes:
                nodes[tgt] = {"id": tgt, "label": m.strip()[:70], "type": "concept",
                              "source": ""}
                added_nodes += 1
            if src != tgt:
                edges.append((src, tgt, "wikilink"))
                added_edges += 1
    print(f"  wikilinks: +{added_nodes} nodes, +{added_edges} edges "
          f"(from {len(md_files)} notes)")


def layout_3d(ids: list, edge_idx: list, iterations: int, seed: int = 7) -> np.ndarray:
    """Force-directed layout: Fruchterman-Reingold with Barnes-Hut-ish repulsion
    approximated by a grid-free O(n^2) pass (fine at this scale) plus spring
    attraction along edges. Returns an (n, 3) array scaled to a walkable room."""
    n = len(ids)
    rng = np.random.default_rng(seed)
    pos = rng.normal(0, 1.0, (n, 3)).astype(np.float32)
    if n < 2:
        return pos

    k = (1.0 / n) ** (1 / 3) * 2.4            # ideal edge length
    src = np.array([e[0] for e in edge_idx], dtype=np.int32)
    dst = np.array([e[1] for e in edge_idx], dtype=np.int32)
    temp = 0.12

    for it in range(iterations):
        # --- repulsion (all pairs) ---
        diff = pos[:, None, :] - pos[None, :, :]
        dist2 = np.sum(diff * diff, axis=-1) + 1e-6
        np.fill_diagonal(dist2, np.inf)
        rep = (k * k) / dist2
        disp = np.einsum("ijk,ij->ik", diff, rep)

        # --- attraction (along edges) ---
        if len(src):
            d = pos[dst] - pos[src]
            dist = np.linalg.norm(d, axis=1, keepdims=True) + 1e-6
            f = (dist / k) * d / dist
            np.add.at(disp, src, f)
            np.add.at(disp, dst, -f)

        # --- gentle pull to origin so nothing drifts to infinity ---
        disp -= pos * 0.012

        norm = np.linalg.norm(disp, axis=1, keepdims=True) + 1e-9
        pos += (disp / norm) * np.minimum(norm, temp)
        temp *= 0.985
        if it % 50 == 0:
            print(f"    layout {it}/{iterations}", end="\r", flush=True)

    # scale into a room roughly 16 m across, centred at origin
    pos -= pos.mean(axis=0)
    span = np.abs(pos).max()
    if span > 0:
        pos *= 8.0 / span
    print(" " * 40, end="\r")
    return pos


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the 3D neural map for the VR room")
    ap.add_argument("--include-wikilinks", action="store_true")
    ap.add_argument("--iterations", type=int, default=300)
    ap.add_argument("--max-nodes", type=int, default=2500,
                    help="cap for headset frame budget; keeps highest-degree nodes")
    args = ap.parse_args()

    print("--- 🕸️  BUILDING THE NEURAL MAP ---")
    nodes, edges = load_graphify()
    if args.include_wikilinks:
        load_wikilinks(nodes, edges)

    # degree, used for both pruning and visual weight
    degree = Counter()
    for s, t, _ in edges:
        degree[s] += 1
        degree[t] += 1

    # drop nodes the graph never references and keep the best-connected
    live = [nid for nid in nodes if degree[nid] > 0]
    live.sort(key=lambda i: -degree[i])
    if len(live) > args.max_nodes:
        print(f"  pruning {len(live)} -> {args.max_nodes} highest-degree nodes")
        live = live[: args.max_nodes]
    keep = set(live)
    edges = [(s, t, l) for s, t, l in edges if s in keep and t in keep]

    # dedupe edges
    seen, uniq = set(), []
    for s, t, l in edges:
        key = (s, t) if s < t else (t, s)
        if key not in seen:
            seen.add(key)
            uniq.append((s, t, l))
    edges = uniq

    ids = live
    index = {nid: i for i, nid in enumerate(ids)}
    edge_idx = [(index[s], index[t]) for s, t, _ in edges]
    print(f"  laying out {len(ids)} nodes / {len(edges)} edges "
          f"({args.iterations} iterations)")
    pos = layout_3d(ids, edge_idx, args.iterations)

    maxdeg = max((degree[i] for i in ids), default=1)
    out = {
        "generated_from": "graphify" + ("+wikilinks" if args.include_wikilinks else ""),
        "counts": {"nodes": len(ids), "edges": len(edges)},
        "types": dict(Counter(nodes[i]["type"] for i in ids).most_common()),
        "nodes": [
            {
                "i": idx,
                "label": nodes[nid]["label"],
                "type": nodes[nid]["type"],
                "source": nodes[nid]["source"],
                "deg": degree[nid],
                "w": round(degree[nid] / maxdeg, 3),
                "p": [round(float(v), 3) for v in pos[idx]],
            }
            for idx, nid in enumerate(ids)
        ],
        "edges": [[index[s], index[t]] for s, t, _ in edges],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")

    print(f"\n--- ✅ NEURAL MAP BUILT ---")
    print(f"   nodes: {len(ids):,}   edges: {len(edges):,}")
    print(f"   types: {out['types']}")
    print(f"   hubs : " + ", ".join(nodes[i]['label'][:34] for i in ids[:3]))
    print(f"   file : {OUT}  ({OUT.stat().st_size/1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
