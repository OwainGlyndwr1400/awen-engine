"""
add_docs.py — add individual documents to a lane, incrementally.

ingest_memory.py rebuilds a lane's ledger from scratch (`open(out, "w")`), which
means the FAISS index has to be rebuilt with it. That is the right tool for a
full harvest and the wrong one for "I wrote two new papers".

This appends instead, through the engine's /add_entry endpoint, so the ledger
and the index stay in step and the engine's own hash set skips anything already
present. Re-running it on the same files is a no-op.

It reuses ingest_memory's cleaner, chunker and quality gate, so a document added
here is chunked identically to one that arrived via a full harvest — same
~1500-char targets, same `[folder/file]` tag, same garble rejection.

Files are also COPIED into Memory/<dest>/ so a future full rebuild still
contains them. Without that they would silently vanish the next time
ingest_memory.py runs.

    py -3.11 add_docs.py "C:\\path\\to\\folder"                  # dry run
    py -3.11 add_docs.py "C:\\path\\to\\folder" --commit
    py -3.11 add_docs.py "C:\\path\\to\\folder" --commit --dest research
"""

import argparse
import json
import shutil
import time
import sys
import urllib.error
import urllib.request
from pathlib import Path

import ingest_memory as im   # reuse the real cleaner/chunker/gate

ROOT = Path(__file__).resolve().parent
ENGINE = "http://127.0.0.1:5000"


def post(path, payload, timeout=600, retries=4):
    """POST with patience and retries.

    A bulk ingest competes with the dream cycle for the engine's db_lock, and a
    dream holds that lock across its LLM synthesis call — which has a 240s
    timeout of its own. A client that waits only 180s therefore gives up on a
    lock the engine is still legitimately holding, and the whole run dies
    mid-file. Wait longer than the dream can possibly take, and retry rather
    than abort: /add_entry is idempotent (hash-deduped), so a retry after a
    partial success simply reports the chunk as already present.
    """
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(
            ENGINE + path, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
            if attempt < retries - 1:
                wait = 5 * (attempt + 1)
                print(f"    ... engine busy ({type(e).__name__}), retry in {wait}s",
                      flush=True)
                time.sleep(wait)
    raise last


def main() -> int:
    ap = argparse.ArgumentParser(description="Add documents to a lane, incrementally")
    ap.add_argument("source", help="file or folder of .md/.txt to add")
    ap.add_argument("--profile", default="knowledge",
                    choices=["knowledge", "shared"],
                    help="target lane (never 'conversations' — that lane is chat only)")
    ap.add_argument("--dest", default="research",
                    help="subfolder under Memory/ to copy originals into")
    ap.add_argument("--commit", action="store_true",
                    help="actually write; without this it is a dry run")
    ap.add_argument("--no-copy", action="store_true",
                    help="do not copy originals into Memory/. Use for large "
                         "corpora already living somewhere stable — copying a "
                         "434 MB book library just to satisfy a future rebuild "
                         "is not worth the disk.")
    ap.add_argument("--node", default="lumos", help="admin node used for the write")
    args = ap.parse_args()

    src = Path(args.source)
    if not src.exists():
        print(f"❌ not found: {src}")
        return 1
    files = sorted(p for p in ([src] if src.is_file() else src.rglob("*"))
                   if p.is_file() and p.suffix.lower() in {".md", ".txt"})
    if not files:
        print("❌ no .md/.txt files found")
        return 1

    # Only a real write needs the engine — it owns the ledger AND the index, and
    # writing one without the other is how alignment breaks. A dry run just
    # chunks locally, so it should work with everything shut down.
    if args.commit:
        try:
            h = json.loads(urllib.request.urlopen(ENGINE + "/health", timeout=15).read())
            if h.get("status") != "OK":
                print(f"❌ engine not ready: {h.get('status')}")
                return 1
            if args.profile not in h.get("loaded_profiles", []):
                print(f"❌ lane '{args.profile}' not loaded. Loaded: {h.get('loaded_profiles')}")
                return 1
        except Exception as e:
            print(f"❌ engine unreachable at {ENGINE}: {e}")
            print("   Start it first — /add_entry keeps the ledger and index in step.")
            return 1

    dest_dir = ROOT / "Memory" / args.dest
    print(f"{'ADDING' if args.commit else 'DRY RUN'} -> lane '{args.profile}', "
          f"originals -> Memory/{args.dest}/\n")

    grand = added = dupes = rejected = 0
    for path in files:
        tag = f"{args.dest}/{path.stem}"
        text = im.clean_text(path.read_text(encoding="utf-8", errors="replace"))
        if not text:
            print(f"  {path.name}: empty after cleaning — skipped")
            continue

        chunks = [c for c in im.chunk_text(text, tag)]
        good = [c for c in chunks if im.passes_quality_gate(c)]
        rejected += len(chunks) - len(good)
        grand += len(good)
        print(f"  {path.name[:64]}", flush=True)
        print(f"    {len(text):,} chars -> {len(chunks)} chunks, {len(good)} pass the gate")

        if not args.commit:
            continue

        if not args.no_copy:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest_dir / path.name)

        a = d = 0
        for c in good:
            try:
                r = post("/add_entry", {"text": c, "profile": args.profile,
                                        "node": args.node,
                                        "source": f"add_docs ({path.name})"})
                # the engine returns success=False for a duplicate hash
                if r.get("status") == "success":
                    a += 1
                else:
                    d += 1
            except urllib.error.HTTPError as e:
                print(f"    ❌ HTTP {e.code}: {e.read().decode()[:120]}")
                return 1
            except Exception as e:
                print(f"    ❌ {e}")
                return 1
        added += a
        dupes += d
        print(f"    added {a}, already present {d}", flush=True)

    print(f"\n{'-'*58}")
    print(f"  files            {len(files)}")
    print(f"  chunks passing   {grand}")
    print(f"  rejected (gate)  {rejected}")
    if args.commit:
        print(f"  ADDED            {added}")
        print(f"  already present  {dupes}")
        try:
            post("/flush", {})
            print("  index flushed to disk.")
        except Exception as e:
            print(f"  ⚠️ flush failed ({e}) — the ledger is durable; the index "
                  f"flushes on its own timer and at shutdown.")
        print("\n  Re-run build_atlas.py when convenient to fold these into the map.")
    else:
        print("\n  Dry run. Add --commit to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
