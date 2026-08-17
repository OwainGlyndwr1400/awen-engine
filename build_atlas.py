"""
build_atlas.py — cluster the LIVING memory, not the library catalogue.

The Neural Map currently draws the graphify document graph: 1,132 nodes of
papers and concepts. That is the catalogue. The engine does not dream over the
catalogue — it dreams over ~297,000 vectors. This clusters those, so the map can
show the memory that is actually in use.

Borrowed from LumOS's ATLAS (k-means over FAISS, centroid-similarity edges,
persisted chunk->cluster assignment). Two things did NOT port and are done
differently, deliberately:

  * LumOS labels each cluster by its most frequent SOURCE TITLE. Our chunks are
    bare JSON strings with no title metadata, so instead each cluster is labelled
    by its most DISTINCTIVE terms — terms over-represented in the cluster against
    the corpus baseline. Readable, and it does not invent provenance we lack.
  * Positions come from PCA of the centroids (deterministic, no layout solver).
    The doc graph gets force-directed layout in build_neural_map.py; 60-100
    centroids do not need it.

Outputs
  docs/atlas.json          clusters + edges + 3D positions   (small, served)
  atlas_assign_<lane>.npy  int32 cluster id per vector       (local, not served)

The .npy files let the engine return a cluster id with every search hit, which
is what drives the activation flash in the map. Without them the map still
renders; it just cannot light up on retrieval.

Run:  py -3.11 build_atlas.py            (add --sample 60000 if RAM is tight)
"""

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import faiss
import numpy as np

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "docs" / "atlas.json"

LANES = {
    # conversations rides an IndexFlatIP over L2-normalised vectors. k-means on
    # normalised vectors is effectively spherical k-means, which is what we want
    # for cosine space — no special-casing needed here.
    "conversations": dict(faiss="conversations_index.faiss",
                          entries="conversations_entries.jsonl", k=50, colour="#f5b95e"),
    "knowledge":     dict(faiss="knowledge_memory_index.faiss",
                          entries="knowledge_entries.jsonl", k=40, colour="#2ef5c8"),
    "shared":        dict(faiss="shared_memory_index.faiss",
                          entries="shared_entries.jsonl", k=60, colour="#39e6ff"),
}

# Enough of a stop list to keep labels meaningful. Aether Scope's emerging-terms
# report leaks 'nthe', 'have', 'like', 'through' into its top 30 — that is what
# happens without one, and the signal drowns.
STOP = set("""
the of and to in a is that it for on as with was be by are this or from at an
not but have has had were which their they them its his her our your you we i
can will would could should may might must do does did been being if then than
when what who how why all any some more most other into over under such no nor
only own same so too very just also about after before between both during each
few further here there once again against above below off out up down only very
these those am he she him who whom whose because until while
one two three four five six seven eight nine ten
also thus hence therefore however moreover within without upon via per
use used using uses new non pre post via etc ie eg
""".split())

TOKEN = re.compile(r"[a-z][a-z0-9\-']{2,}")


def load_lane(name, cfg, sample):
    fp, ep = ROOT / cfg["faiss"], ROOT / cfg["entries"]
    if not fp.exists():
        print(f"  ! {name}: {cfg['faiss']} missing — skipping")
        return None
    index = faiss.read_index(str(fp))
    n = index.ntotal
    if n == 0:
        print(f"  ! {name}: empty index — skipping")
        return None

    chunks = []
    if ep.exists():
        with open(ep, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                chunks.append(rec if isinstance(rec, str) else rec.get("text", ""))

    if len(chunks) != n:
        # The engine enforces ledger/vector alignment at load; if they have
        # drifted, trust the vectors and truncate rather than mislabel.
        print(f"  ! {name}: {len(chunks)} ledger entries vs {n} vectors — "
              f"truncating labels to {min(len(chunks), n)}")
    print(f"  {name}: {n:,} vectors, dim {index.d}")
    return dict(index=index, n=n, chunks=chunks, **cfg)


def cluster_lane(name, lane, sample, seed=1400):
    n, d, k = lane["n"], lane["index"].d, min(lane["k"], max(2, lane["n"] // 20))
    print(f"\n  {name}: k-means k={k} over {n:,} vectors")

    train_n = min(n, sample) if sample else n
    if train_n < n:
        rs = np.random.RandomState(seed)
        pick = np.sort(rs.choice(n, train_n, replace=False))
        train = np.vstack([lane["index"].reconstruct(int(i)) for i in pick])
        print(f"    training on a {train_n:,}-vector sample")
    else:
        train = lane["index"].reconstruct_n(0, n)

    km = faiss.Kmeans(d, k, niter=25, seed=seed, verbose=False)
    km.train(train.astype("float32"))
    del train

    # assign every vector, in blocks so a 1.1 GB lane does not have to be resident
    assign = np.empty(n, dtype=np.int32)
    BLOCK = 20000
    for s in range(0, n, BLOCK):
        e = min(s + BLOCK, n)
        _, idx = km.index.search(lane["index"].reconstruct_n(s, e - s).astype("float32"), 1)
        assign[s:e] = idx[:, 0]
    return km.centroids.copy(), assign, k


def label_clusters(chunks, assign, k, baseline):
    """Label each cluster by its most CHARACTERISTIC terms.

    A plain rate/baseline ratio does not work here: it is won outright by
    hapaxes and OCR garble ('zoroafter's', 'zinctum'), because a term seen 3
    times in one cluster and nowhere else scores near-infinity. First pass at
    this produced exactly that.

    So score by the term's contribution to the KL divergence of the cluster
    against the lane baseline:

        score = p_c * log(p_c / p_all)

    which rewards terms that are BOTH common inside the cluster and lifted
    above baseline, with two hard floors: the term must appear in a real
    fraction of the cluster, and must be attested widely enough across the lane
    to not be scanning noise.
    """
    import math

    N = max(1, len(chunks))
    labels, tops = [], []
    per = [Counter() for _ in range(k)]
    tot = [0] * k
    for i, txt in enumerate(chunks):
        c = int(assign[i])
        per[c].update(set(t for t in TOKEN.findall(txt.lower()) if t not in STOP))
        tot[c] += 1

    MIN_LANE_DF = max(25, N // 20000)   # term must be attested across the lane
    for c in range(k):
        if not tot[c]:
            labels.append("(empty)"); tops.append([]); continue
        min_c_df = max(4, int(0.04 * tot[c]))     # and be common in the cluster
        scored = []
        for term, cnt in per[c].items():
            if cnt < min_c_df:
                continue
            df_all = baseline.get(term, 0)
            if df_all < MIN_LANE_DF:
                continue
            p_c, p_all = cnt / tot[c], df_all / N
            if p_all <= 0 or p_c <= p_all:        # not actually enriched
                continue
            scored.append((p_c * math.log(p_c / p_all), term, cnt))
        scored.sort(reverse=True)
        top = [t for _, t, _ in scored[:6]]
        tops.append(top)
        labels.append(" · ".join(top[:3]) if top else "(diffuse)")
    return labels, tops


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0,
                    help="train k-means on N sampled vectors (0 = all)")
    args = ap.parse_args()

    print("ATLAS — clustering the live index\n")
    lanes = {}
    for name, cfg in LANES.items():
        got = load_lane(name, cfg, args.sample)
        if got:
            lanes[name] = got
    if not lanes:
        print("\nNo indices found. Nothing to cluster."); sys.exit(1)

    clusters, all_cent = [], []
    for name, lane in lanes.items():
        cent, assign, k = cluster_lane(name, lane, args.sample)
        np.save(ROOT / f"atlas_assign_{name}.npy", assign)

        chunks = lane["chunks"][:lane["n"]]
        if len(chunks) == lane["n"]:
            baseline = Counter()
            for txt in chunks:
                baseline.update(set(t for t in TOKEN.findall(txt.lower()) if t not in STOP))
            labels, tops = label_clusters(chunks, assign, k, baseline)
        else:
            labels = [f"{name} cluster {c}" for c in range(k)]
            tops = [[] for _ in range(k)]
            print(f"    (no aligned ledger text — clusters left unlabelled)")

        sizes = np.bincount(assign, minlength=k)
        for c in range(k):
            clusters.append(dict(id=f"{name}:{c}", lane=name, k=int(c),
                                 size=int(sizes[c]), label=labels[c],
                                 terms=tops[c], colour=lane["colour"]))
            all_cent.append(cent[c])
        print(f"    {k} clusters · largest {int(sizes.max()):,} · smallest {int(sizes.min()):,}")
        print(f"    e.g. {labels[int(np.argmax(sizes))]!r}")

    # --- positions: PCA of the centroids, deterministic --------------------
    C = np.vstack(all_cent).astype("float64")
    C -= C.mean(0)
    _, _, Vt = np.linalg.svd(C, full_matrices=False)
    P = C @ Vt[:3].T
    P /= (np.abs(P).max() or 1.0)
    for i, cl in enumerate(clusters):
        cl["pos"] = [round(float(v), 4) for v in P[i]]

    # --- edges: centroid cosine, intra-lane top-5, cross-lane top-3 --------
    N = np.vstack(all_cent).astype("float32")
    N /= (np.linalg.norm(N, axis=1, keepdims=True) + 1e-9)
    S = N @ N.T
    np.fill_diagonal(S, -1.0)
    edges, seen = [], set()
    for i, ci in enumerate(clusters):
        same = [j for j in range(len(clusters)) if clusters[j]["lane"] == ci["lane"] and j != i]
        cross = [j for j in range(len(clusters)) if clusters[j]["lane"] != ci["lane"]]
        for pool, lim, floor in ((same, 5, -1.0), (cross, 3, 0.5)):
            for j in sorted(pool, key=lambda j: -S[i, j])[:lim]:
                if S[i, j] < floor:
                    continue
                key = (min(i, j), max(i, j))
                if key in seen:
                    continue
                seen.add(key)
                edges.append(dict(a=clusters[i]["id"], b=clusters[j]["id"],
                                  w=round(float(S[i, j]), 4),
                                  cross=clusters[i]["lane"] != clusters[j]["lane"]))

    payload = dict(
        generated=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        vectors=sum(l["n"] for l in lanes.values()),
        clusters=clusters, edges=edges,
        note=("Clusters of the live FAISS index — the memory the engine actually "
              "dreams over, not the graphify document graph. Labels are the terms "
              "most over-represented in each cluster against the corpus baseline; "
              "our chunks carry no title metadata, so no provenance is claimed."),
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    print(f"\nwrote {OUT}  ({OUT.stat().st_size/1024:.1f} KB)")
    print(f"  {len(clusters)} clusters · {len(edges)} edges · "
          f"{payload['vectors']:,} vectors")
    print(f"  assignments: atlas_assign_*.npy (local, for the retrieval flash)")


if __name__ == "__main__":
    main()
