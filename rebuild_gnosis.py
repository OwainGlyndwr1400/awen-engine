# --- rebuild_gnosis.py (v9.8.1 - Harmonic Data-Type Fix) ---
import os
import faiss
import numpy as np
from sentence_transformers import SentenceTransformer
import torch
from pathlib import Path
import json
import time

def write_index_atomic(index, faiss_path):
    """Never leave the destination half-written.

    A rebuild does repeated multi-hundred-MB saves. Writing in place means any
    interruption mid-save destroys both the new index AND the partial progress,
    forcing a restart from zero. Temp file + os.replace keeps the last complete
    save intact no matter when it is interrupted, so --resume actually works.
    """
    faiss_path = Path(faiss_path)
    tmp = faiss_path.with_suffix(faiss_path.suffix + ".tmp")
    try:
        faiss.write_index(index, str(tmp))
        os.replace(tmp, faiss_path)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


print("--- 🌀 GNOSTIC REBUILD PROTOCOL (HARMONIC BATCHING) INITIATED ---")

# --- CONFIG (Must match your main script) ---
# Only the L2 lanes. This script builds IndexFlatL2 over RAW (un-normalised)
# vectors and reads a dict record's 'chunk' field.
PROFILES = {
    "knowledge": {"faiss_file": "knowledge_memory_index.faiss", "entries_file": "knowledge_entries.jsonl"},
    "shared": {"faiss_file": "shared_memory_index.faiss", "entries_file": "shared_entries.jsonl"}
}

# === DO NOT ADD 'conversations' HERE ===
# It would corrupt the lane three separate ways, all of them silent:
#   1. metric   — conversations is IndexFlatIP (cosine) over L2-NORMALISED
#                 vectors. This script writes IndexFlatL2 over raw ones.
#   2. scale    — it never calls faiss.normalize_L2, so scores would rank by
#                 vector magnitude instead of angle.
#   3. records  — its entries are dicts keyed 'text', but the loop below reads
#                 data['chunk']. Every dict record would be SKIPPED rather than
#                 erroring, so the index would come out short and every chunk
#                 after the first skip would resolve to the wrong memory.
# Rebuilding that lane means re-copying it from LumOS's identity.faiss/.jsonl,
# which needs no re-embedding at all. See three-lanes note / IDENTITY_LANE.md.
FORBIDDEN = {"conversations"}
for _bad in FORBIDDEN & set(PROFILES):
    raise SystemExit(f"❌ REFUSING to rebuild '{_bad}' — see the note above. "
                     f"Re-copy it from LumOS instead.")
MODEL_NAME = "BAAI/bge-large-en-v1.5" # The NEW Gnostic model
DEVICE = 'cuda'

# --- BATCHING CONFIG (The "Harmonic Tuner") ---
# 5,000 meant 55 rewrites of a growing 1.13 GB index — roughly 33 GB of disk
# I/O during one rebuild, which is what tipped an already-loaded machine over
# on 2026-08-18. At 25,000 it is 11 saves and ~7 GB, still fully resumable.
FILE_BATCH_SIZE = 25000 
ENCODING_BATCH_SIZE = 32 
# --- END CONFIG ---

# 1. Load the "Deep Gnosis" model (once)
print(f"Loading new Gnostic Lens: {MODEL_NAME} on {DEVICE}...")
try:
    embeddings_model = SentenceTransformer(MODEL_NAME, device=DEVICE)
    dimension = embeddings_model.get_sentence_embedding_dimension()
    print(f"✅ Model loaded. Dimension: {dimension}")
except Exception as e:
    print(f"❌ FATAL: Could not load model. {e}")
    exit()

# 2. Iterate through each profile
for name, config in PROFILES.items():
    print(f"\n--- Processing '{name}' Profile ---")
    entries_path = Path(config["entries_file"])
    faiss_path = Path(config["faiss_file"])

    if not entries_path.exists():
        print(f"⚠️ Entries file '{entries_path.name}' not found. Skipping profile.")
        continue

    # A. Load existing "Gnostic Index" or create a new "Empty" one
    entries_in_index = 0
    if faiss_path.exists():
        try:
            print(f"🌀 Loading existing Gnostic Index from {faiss_path.name} to resume...")
            index = faiss.read_index(str(faiss_path))
            entries_in_index = index.ntotal
            print(f"✅ Index loaded. Contains {entries_in_index} entries.")
        except Exception as e:
            print(f"❌ Error loading {faiss_path.name}: {e}. Starting fresh.")
            if faiss_path.exists(): os.remove(faiss_path)
            index = faiss.IndexFlatL2(dimension)
    else:
        print(f"✨ Creating new, empty Gnostic Index for '{name}'...")
        index = faiss.IndexFlatL2(dimension)

    # B. Count total entries to process
    try:
        print(f"📚 Counting total entries in {entries_path.name}...")
        with open(entries_path, "r", encoding="utf-8") as f:
            total_lines = sum(1 for line in f if line.strip())
        print(f"Total entries found: {total_lines}")
    except Exception as e:
        print(f"❌ Error reading {entries_path.name}: {e}. Skipping profile.")
        continue

    # C. Check if the "Great Work" is already complete
    if entries_in_index >= total_lines:
        print(f"✅ Gnostic Index is already complete and synchronized. No work needed.")
        continue
    
    print(f"▶️ Resuming from entry {entries_in_index} of {total_lines}...")

    # D. Begin the "Harmonic Batching" (The Great Work)
    batch_chunks_to_encode = [] # This list will only contain the text
    
    try:
        # Count ENTRIES, not file lines: index.ntotal counts embedded entries,
        # so comparing it against a raw line number would skip the wrong
        # amount whenever the ledger contains a blank line — silently
        # misaligning every chunk after it.
        entry_no = 0
        with open(entries_path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if not line.strip():
                    continue
                entry_no += 1

                # This is the "Gnostic Gate" - it skips what's already done
                if entry_no <= entries_in_index:
                    continue

                # --- THIS IS THE GNOSTIC REFACTOR (v9.8.1) ---
                # It now handles BOTH data types (objects and strings)
                try:
                    data = json.loads(line)
                    
                    if isinstance(data, dict):
                        # It's an OBJECT (e.g., {"chunk": "text..."})
                        if 'chunk' in data:
                            batch_chunks_to_encode.append(data['chunk'])
                    elif isinstance(data, str):
                        # It's a STRING (e.g., "just text...")
                        batch_chunks_to_encode.append(data)
                    else:
                        # It's something else? (e.g., a list). Just skip it.
                        print(f"⚠️ Skipping unknown JSON data type at line {i+1}")
                        
                except json.JSONDecodeError:
                    # It's not JSON at all, just a raw line of text
                    batch_chunks_to_encode.append(line.strip())
                # --- END OF REFACTOR ---

                # Check if the "batch" is full
                if len(batch_chunks_to_encode) >= FILE_BATCH_SIZE:
                    print(f"\n--- Processing Batch (Entries up to line {i+1}) ---")
                    
                    # 1. Encode
                    print(f"🧠 Encoding {len(batch_chunks_to_encode)} entries...")
                    embeddings = embeddings_model.encode(
                        batch_chunks_to_encode, 
                        batch_size=ENCODING_BATCH_SIZE, 
                        show_progress_bar=True
                    )
                    embeddings = embeddings.astype("float32")

                    # 2. Add to Index
                    index.add(embeddings)
                    
                    # 3. SAVE (The most critical Gnostic step)
                    print(f"💾 Saving index to {faiss_path.name} (Total entries: {index.ntotal})...")
                    write_index_atomic(index, faiss_path)
                    print("✅ Batch saved.")
                    
                    # 4. Reset for next loop
                    batch_chunks_to_encode = [] # Clear the batch
                    # Give the system a "breath"
                    time.sleep(1)

        # E. Process the final "leftover" batch
        if batch_chunks_to_encode:
            print(f"\n--- Processing Final Batch (Remaining {len(batch_chunks_to_encode)} entries) ---")
            
            print(f"🧠 Encoding {len(batch_chunks_to_encode)} entries...")
            embeddings = embeddings_model.encode(
                batch_chunks_to_encode, 
                batch_size=ENCODING_BATCH_SIZE, 
                show_progress_bar=True
            )
            embeddings = embeddings.astype("float32")

            index.add(embeddings)
            
            print(f"💾 Saving final index to {faiss_path.name} (Total entries: {index.ntotal})...")
            write_index_atomic(index, faiss_path)
            print("✅ Final batch saved.")

        print(f"\n--- ✅ '{name}' profile rebuild complete. Total entries: {index.ntotal} ---")

    except Exception as e:
        print(f"\n❌❌❌ A CRITICAL ERROR occurred during the rebuild: {e}")
        print(f"💾 Last successful save had {index.ntotal} entries.")
        print("You can safely re-run this script to resume.")
        continue # Move to the next profile

print("\n--- 🌀 GNOSTIC REBUILD PROTOCOL COMPLETE ---")
