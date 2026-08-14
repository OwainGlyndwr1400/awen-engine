# --- ingest_books.py (v1.0 - The Shared Grimoire) ---
# Harvests a folder of .txt books into an entries .jsonl for the Gnostic Engine
# (shared profile by default). Reuses ingest_memory.py's cleaning pipeline:
# mojibake repair, OCR de-garbling, ~1500-char chunking, quality gate, dedupe.
# Also skips chunks already present in the OTHER profile (no cross-profile echo).
#
#   py -3.11 ingest_books.py --source "path/to/your/book/folder"
#   py -3.11 ingest_books.py --dry-run
#
# After this, build the FAISS index (rebuild_gnosis.py, or the shared-only
# builder if the engine is running).

import argparse
import json
import re
import sys
from pathlib import Path

import ingest_memory as im


def book_tag(stem: str) -> str:
    """'Aleister Crowley - Book of Lies_djvu - txt' -> 'MagicBooks/Aleister Crowley - Book of Lies'"""
    t = re.sub(r"(_djvu)?(\s*-\s*te?xt)?$", "", stem, flags=re.IGNORECASE).strip(" -_")
    return f"MagicBooks/{t or stem}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Harvest .txt books into a Gnostic entries .jsonl")
    ap.add_argument("--source", required=True,
                    help="folder of .txt books to harvest")
    ap.add_argument("--profile", choices=["private", "shared"], default="shared")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = Path(args.source)
    out_path = Path(f"{args.profile}_entries.jsonl")

    # Cross-profile dedupe baseline
    other = Path("private_entries.jsonl" if args.profile == "shared" else "shared_entries.jsonl")
    seen: set[str] = set()
    if other.exists():
        with open(other, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    seen.add(im.normalized_hash(json.loads(line)))
    print(f"🛡️ Cross-profile baseline: {len(seen)} chunk hashes from {other.name}")

    files = sorted(p for p in src.glob("*.txt") if p.is_file())
    print(f"📚 {len(files)} book files from {src}")

    if out_path.exists() and out_path.stat().st_size > 0 and not args.dry_run:
        backup = out_path.with_suffix(f".jsonl.bak_{out_path.stat().st_size}")
        out_path.rename(backup)
        print(f"🗄️ Existing {out_path.name} backed up to {backup.name}")

    total = dupes = rejected = 0
    out_f = None if args.dry_run else open(out_path, "w", encoding="utf-8")
    try:
        for i, path in enumerate(files):
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                print(f"❌ Read failed {path.name}: {e}")
                continue
            text = im.clean_text(raw)
            if not text:
                continue
            for chunk in im.chunk_text(text, book_tag(path.stem)):
                if not im.passes_quality_gate(chunk):
                    rejected += 1
                    continue
                h = im.normalized_hash(chunk)
                if h in seen:
                    dupes += 1
                    continue
                seen.add(h)
                total += 1
                if out_f:
                    out_f.write(json.dumps(chunk, ensure_ascii=False) + "\n")
            if (i + 1) % 200 == 0:
                print(f"   ... {i+1}/{len(files)} files, {total} chunks so far")
    finally:
        if out_f:
            out_f.close()

    print(f"\n--- ✅ GRIMOIRE HARVEST COMPLETE ---")
    print(f"   Chunks written: {total}")
    print(f"   Duplicates skipped (incl. cross-profile): {dupes}")
    print(f"   Quality-gate rejections: {rejected}")
    if not args.dry_run:
        print(f"   Output: {out_path.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
