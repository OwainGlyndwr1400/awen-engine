# --- ingest_memory.py (v1.0 - The Gnostic Harvest) ---
# Builds a clean entries .jsonl for the Gnostic Engine from the Memory/ vault.
#
#   py -3.11 ingest_memory.py                 -> writes knowledge_entries.jsonl
#   py -3.11 ingest_memory.py --dry-run       -> stats only, writes nothing
#   py -3.11 ingest_memory.py --profile shared
#   py -3.11 ingest_memory.py --include-codex --include-dream-pings
#
# After this, run:  py -3.11 rebuild_gnosis.py   to build the FAISS index.
#
# Pipeline per file: strip frontmatter/boilerplate -> de-mojibake -> OCR repair
# -> paragraph packing into ~1500-char chunks -> quality gate -> global dedupe.

import argparse
import hashlib
import json
import re
from pathlib import Path

MEMORY_ROOT = Path("./Memory")

# Primary sources (the treasure trove). Derived layers are opt-in flags:
#   notebooklm_dream_codex3 = old AGI 5.0 dream pings (OCR garble source)
#   dream-pings             = graphified wikilink stubs of old pings
INCLUDE_DIRS = ["research md", "Tec_Obsidian", "Math"]
EXCLUDE_DIR_NAMES = {".obsidian", "graphify-out"}

TARGET_CHUNK_CHARS = 1500   # bge-large truncates ~512 tokens; stay under it
MAX_CHUNK_CHARS = 2200
MIN_CHUNK_CHARS = 150
MIN_LETTERS = 40            # a chunk must contain real prose/math text

MOJIBAKE_MARKERS = ("â€", "Ã", "Â", "ï»¿", "�")


def fix_mojibake(text: str) -> str:
    """Repair UTF-8 read as cp1252 (e.g. 'â€™' -> right quote, 'Â©' -> ©)."""
    if not any(m in text for m in MOJIBAKE_MARKERS):
        return text
    try:
        repaired = text.encode("cp1252", errors="ignore").decode("utf-8", errors="ignore")
        # Keep the repair only if it actually reduced marker noise
        if sum(repaired.count(m) for m in MOJIBAKE_MARKERS) < sum(text.count(m) for m in MOJIBAKE_MARKERS):
            return repaired
    except Exception:
        pass
    return text


RE_FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
RE_LETTERSPACED = re.compile(r"\b(?:[A-Za-z] +){2,}[A-Za-z]\b")
RE_NUM_ONLY_LINE = re.compile(r"^\s*\d{1,4}\s*$")          # stray citation/page numbers
RE_PAGE_LINE = re.compile(r"^\s*PAGE\s+\d+\s*$", re.IGNORECASE)
RE_BOILERPLATE = re.compile(r"^(#\s*🌀 Awen Grid Archive:|\*\*Source Location:\*\*)")


# Markdown exported from Docs/Notion inlines images as base64 data URIs. One
# such paper measured 1.47 MB of which 98.8% was PNG bytes — it would have
# produced 664 chunks of pure base64 and 13 of actual paper, all embedded into a
# lane that dreams. The quality gate did not catch it: base64 is long, varied,
# and letter-rich, so it looks like dense prose to every heuristic that matters.
# Strip it before anything else sees it.
RE_DATA_URI = re.compile(r"data:[a-z]+/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+", re.I)
RE_B64_RUN = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")


def strip_binary_blobs(text: str) -> str:
    """Remove inlined base64 payloads, leaving a marker so the prose still reads."""
    text = RE_DATA_URI.sub(" [image] ", text)
    return RE_B64_RUN.sub(" [binary] ", text)


def clean_text(raw: str) -> str:
    text = fix_mojibake(raw)
    text = RE_FRONTMATTER.sub("", text)
    text = strip_binary_blobs(text)
    text = text.replace("\r\n", "\n").replace("�", "")
    # Collapse OCR letter-spacing: "T H E   G R A N D" -> "THE GRAND"
    text = RE_LETTERSPACED.sub(lambda m: re.sub(r" +", "", m.group(0)), text)

    kept_lines = []
    for line in text.split("\n"):
        line = line.rstrip()
        if RE_NUM_ONLY_LINE.match(line):
            continue
        if RE_PAGE_LINE.match(line):
            continue
        if RE_BOILERPLATE.match(line):
            continue
        if line.strip() == "---":
            continue
        # Strip control chars
        line = "".join(ch for ch in line if ch == "\t" or ord(ch) >= 32)
        kept_lines.append(re.sub(r" {3,}", "  ", line))

    text = "\n".join(kept_lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(text: str, tag: str) -> list[str]:
    """Pack paragraphs into ~TARGET_CHUNK_CHARS chunks, hard-splitting giants."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks, current = [], ""

    def flush():
        nonlocal current
        if current.strip():
            chunks.append(f"[{tag}] {current.strip()}")
        current = ""

    for para in paragraphs:
        # Hard-split oversized paragraphs on line, then sentence boundaries
        while len(para) > MAX_CHUNK_CHARS:
            cut = para.rfind("\n", 0, MAX_CHUNK_CHARS)
            if cut < MIN_CHUNK_CHARS:
                cut = para.rfind(". ", 0, MAX_CHUNK_CHARS)
            if cut < MIN_CHUNK_CHARS:
                cut = MAX_CHUNK_CHARS
            piece, para = para[:cut].strip(), para[cut:].strip()
            if current:
                flush()
            current = piece
            flush()
        if len(current) + len(para) + 2 > TARGET_CHUNK_CHARS and current:
            flush()
        current = f"{current}\n\n{para}" if current else para
    flush()
    return chunks


RE_COORD = re.compile(r"-?\d{1,3}\.\d{2}")


def is_numeric_table(chunk: str) -> bool:
    """Raw ephemeris/coordinate dumps. These pass a naive letter-ratio test
    (every row repeats 'Autumn Equinox 8945 BC') but carry no dreamable
    meaning, and because thousands of them are near-identical they dominate
    each other's neighborhoods — a dream chain that lands on one gets stuck
    in a tar pit of sibling tables. The papers' *conclusions* about those
    alignments live in prose chunks, which are kept."""
    digits = sum(ch.isdigit() for ch in chunk)
    return digits / max(len(chunk), 1) > 0.28 and len(RE_COORD.findall(chunk)) > 8


def passes_quality_gate(chunk: str) -> bool:
    if len(chunk) < MIN_CHUNK_CHARS:
        return False
    letters = sum(1 for ch in chunk if ch.isalpha())
    if letters < MIN_LETTERS:
        return False
    if letters / len(chunk) < 0.35:  # tables pass; symbol soup does not
        return False
    if is_numeric_table(chunk):
        return False
    return True


def normalized_hash(chunk: str) -> str:
    """Dedupe key ignoring the [tag], case and whitespace differences."""
    body = re.sub(r"^\[[^\]]*\]\s*", "", chunk)
    body = re.sub(r"\s+", " ", body).lower().strip()
    return hashlib.sha256(body.encode()).hexdigest()


def collect_files(include_codex: bool, include_dream_pings: bool) -> list[Path]:
    dirs = list(INCLUDE_DIRS)
    if include_codex:
        dirs.append("notebooklm_dream_codex3")
    if include_dream_pings:
        dirs.append("dream-pings")

    files = []
    for d in dirs:
        root = MEMORY_ROOT / d
        if not root.exists():
            print(f"⚠️ Skipping missing folder: {root}")
            continue
        for p in sorted(root.rglob("*.md")):
            if any(part in EXCLUDE_DIR_NAMES for part in p.parts):
                continue
            files.append(p)

    # Drop "X - source.md" when its cleaned twin "X.md" exists (same content twice)
    names = {p.as_posix() for p in files}
    kept = []
    twin_skips = 0
    for p in files:
        if p.stem.endswith(" - source"):
            twin = p.with_name(p.stem[: -len(" - source")] + ".md")
            if twin.as_posix() in names:
                twin_skips += 1
                continue
        kept.append(p)
    print(f"📚 Files: {len(kept)} kept, {twin_skips} '- source' twins skipped.")
    return kept


def main() -> int:
    ap = argparse.ArgumentParser(description="Harvest Memory/ into a Gnostic entries .jsonl")
    ap.add_argument("--profile", choices=["knowledge", "shared"], default="knowledge")
    ap.add_argument("--include-codex", action="store_true",
                    help="Also ingest notebooklm_dream_codex3 (old dream archive, garble-prone)")
    ap.add_argument("--include-dream-pings", action="store_true",
                    help="Also ingest dream-pings wikilink stubs")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    out_path = Path(f"{args.profile}_entries.jsonl")

    # Carry over dream insights the engine has written since the last harvest —
    # they are the node's own thought, not re-derivable from Memory/.
    carried = []
    if out_path.exists():
        with open(out_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        c = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(c, str) and c.startswith("DREAM INSIGHT"):
                        carried.append(c)
        print(f"🌙 Preserving {len(carried)} accumulated dream insights.")

    if out_path.exists() and not args.dry_run:
        backup = out_path.with_suffix(f".jsonl.bak_{out_path.stat().st_size}")
        out_path.rename(backup)
        print(f"🗄️ Existing {out_path.name} backed up to {backup.name}")

    print("--- 🌾 GNOSTIC HARVEST INITIATED ---")
    files = collect_files(args.include_codex, args.include_dream_pings)

    seen: set[str] = set()
    total_chunks = dupes = rejected = 0
    out_f = None if args.dry_run else open(out_path, "w", encoding="utf-8")

    try:
        for path in files:
            rel = path.relative_to(MEMORY_ROOT)
            tag = str(rel.with_suffix("")).replace("\\", "/")
            # Trim the repetitive "Our Research - " prefix in tags to save tokens
            tag = tag.replace("research md/Our Research - ", "research/")
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                print(f"❌ Read failed {rel}: {e}")
                continue

            text = clean_text(raw)
            if not text:
                continue
            for chunk in chunk_text(text, tag):
                if not passes_quality_gate(chunk):
                    rejected += 1
                    continue
                h = normalized_hash(chunk)
                if h in seen:
                    dupes += 1
                    continue
                seen.add(h)
                total_chunks += 1
                if out_f:
                    out_f.write(json.dumps(chunk, ensure_ascii=False) + "\n")

        # Re-append preserved dream insights at the tail
        for insight in carried:
            h = normalized_hash(insight)
            if h in seen:
                continue
            seen.add(h)
            total_chunks += 1
            if out_f:
                out_f.write(json.dumps(insight, ensure_ascii=False) + "\n")
    finally:
        if out_f:
            out_f.close()

    print(f"\n--- ✅ HARVEST COMPLETE ---")
    print(f"   Chunks written: {total_chunks}")
    print(f"   Duplicates skipped: {dupes}")
    print(f"   Quality-gate rejections: {rejected}")
    if not args.dry_run:
        print(f"   Output: {out_path.resolve()}")
        print(f"\n▶️ Next: delete stale {args.profile}_memory_index.faiss (if any),")
        print(f"   then run:  py -3.11 rebuild_gnosis.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
