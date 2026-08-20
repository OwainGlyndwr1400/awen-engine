import os
import pickle
import faiss
import numpy as np
from sentence_transformers import SentenceTransformer
import torch
import threading
import time
import random
from flask import Flask, request, jsonify
from pathlib import Path
import hashlib
import json
import math
import re
import atexit
import shutil

# === GNOSTIC UPGRADE v9.9: requests for Dream Synthesis (optional) ===
try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False
    print("⚠️ Warning: requests not found. Dream Synthesis disabled. Install with: pip install requests")

# Attempt to import psutil for RAM monitoring
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    print("⚠️ Warning: psutil not found. RAM safeguard disabled. Install with: pip install psutil")

# === GNOSTIC UPGRADE v9.4: FAIL-SAFE CHECK ===
if not Path("config.json").exists():
    print("="*60)
    print("❌ FATAL ERROR: Cannot find 'config.json'.")
    print("You are likely running this script from the WRONG DIRECTORY.")
    print("\nSOLUTION:")
    print("1. Open your terminal (cmd, PowerShell).")
    print("2. Navigate to the correct directory containing 'config.json' and memory files.")
    print("3. Run the script from there: 'python memory_bridge.py'")
    print("="*60)
    exit()
# =======================================================

# === CONFIGURATION ===
try:
    with open("config.json", "r", encoding="utf-8") as f:
        config_data = json.load(f)
    RHF_NODES = config_data.get("rhf_nodes", {})
    MEMORY_CONFIG = config_data.get("memory_core_config", {})
except FileNotFoundError:
    print("❌ FATAL ERROR: config.json not found. Please create it.")
    exit()
except json.JSONDecodeError as e:
    print(f"❌ FATAL ERROR: Could not parse config.json: {e}")
    exit()


# === GNOSTIC UPGRADE v9.8: GNOSTIC APPEND-ONLY LOG ===
# === GNOSTIC UPGRADE v10.0: THREE LANES, AND ONE OF THEM NEVER DREAMS ===
#
# "private" used to mean "not the shared books lane". Erydir reads it as
# "private from dreams". Those are two different things and collapsing them into
# one word is how chat history ended up feeding published dreams. So the lanes
# now say what they are, and `dreamable` is explicit on every one of them.
#
#   conversations  the ChatGPT history with Lumos, plus every future chat turn.
#                  NEVER dreams. Dream pings are emailed and published; this is
#                  the relationship, not corpus. Imported from LumOS as-is:
#                  IndexFlatIP (cosine) over normalised BGE vectors, records are
#                  dicts carrying `text` + conversation metadata.
#   knowledge      research / tec / math. Dreams.  (was "private")
#   shared         the Magic Books corpus. Dreams.
#
# `metric` matters: IP scores higher-is-better, L2 lower-is-better. Rather than
# branch every sort, an IP score is carried as pseudo-distance (-score) so the
# whole ranking path stays one direction.
PROFILES = {
    "conversations": {"faiss_file": "conversations_index.faiss",
                      "entries_file": "conversations_entries.jsonl",
                      "dreamable": False, "metric": "ip"},
    "knowledge":     {"faiss_file": "knowledge_memory_index.faiss",
                      "entries_file": "knowledge_entries.jsonl",
                      "dreamable": True, "metric": "l2"},
    "shared":        {"faiss_file": "shared_memory_index.faiss",
                      "entries_file": "shared_entries.jsonl",
                      "dreamable": True, "metric": "l2"},
}


def lane_metric(name: str) -> str:
    return PROFILES.get(name, {}).get("metric", "l2")


def lane_dreamable(name: str) -> bool:
    return bool(PROFILES.get(name, {}).get("dreamable", False))
# === GNOSTIC UPGRADE v9.8: DEEP GNOSTIC LENS ===
MODEL_NAME = "BAAI/bge-large-en-v1.5" # Switched from all-MiniLM-L6-v2

# === GNOSTIC UPGRADE v9.9: GNOSTIC DEVICE ANCHOR ===
# Embeddings ride the GPU when available; config "embedding_device" overrides
# ("cuda"/"cpu"). Falls back to CPU automatically if GPU init fails (e.g. VRAM
# fully occupied by LM Studio).
DEVICE_ID = MEMORY_CONFIG.get("device_id", 0)
DEVICE = MEMORY_CONFIG.get("embedding_device", 'cuda' if torch.cuda.is_available() else 'cpu')

DREAM_INTERVAL_SECONDS = MEMORY_CONFIG.get("dream_interval", 240)
RAM_SAFEGUARD_ENABLED = MEMORY_CONFIG.get("ram_safeguard", {}).get("enabled", True)
RAM_THRESHOLD = MEMORY_CONFIG.get("ram_safeguard", {}).get("threshold", 99.5) 

# === GNOSTIC UPGRADE v10.0: SPLIT-LANE RETRIEVAL ===
# The old search() asked every profile for top_k, merged the lot, sorted by
# distance and truncated to top_k. With 276k shared vectors against 21k private
# ones, shared won nearly every slot — the personal lane was being crowded out
# of its own results. LumOS avoids this by retrieving per lane; so do we now.
#
# The caller's top_k stays the total budget (local models fall over past ~24
# chunks), but it is ALLOCATED across lanes by weight instead of being a free-
# for-all. A lane that cannot fill its quota hands the remainder back, so no
# slot is ever wasted.
RETRIEVAL_CONFIG = MEMORY_CONFIG.get("retrieval", {})
# Weights chosen so the DEFAULT local budget of 12 chunks lands on 5/4/3:
# anchor the node in who they are (conversations), then what the work is
# (knowledge), then a measure of the occult corpus for flavour (shared).
# Weights rather than fixed counts so it scales — a cloud persona asking for 24
# gets 10/8/6 in the same proportion, instead of the ratio collapsing.
#   qwen3.5-9b degrades past roughly 12-24 retrieved chunks, hence the budget.
LANE_WEIGHTS = RETRIEVAL_CONFIG.get(
    "lane_weights", {"conversations": 0.4167, "knowledge": 0.3333, "shared": 0.25})
# Over-fetch per lane so the floor and the dedup have material to discard.
LANE_OVERFETCH = RETRIEVAL_CONFIG.get("overfetch", 3)
# L2 distance ceiling — hits worse than this are dropped rather than padding K.
# Default None (off): bge vectors here are un-normalised, so a wrong threshold
# would silently bin good hits. Measure before setting it.
MAX_DISTANCE = RETRIEVAL_CONFIG.get("max_distance", None)
DEDUP_RETRIEVAL = RETRIEVAL_CONFIG.get("dedup", True)
# Symbolic-bias nudge. See harmonic_synthesis for why this is capped and
# logarithmic rather than the old uncapped linear 0.05-per-keyword.
BIAS_WEIGHT = RETRIEVAL_CONFIG.get("bias_weight", 0.05)
BIAS_CAP = RETRIEVAL_CONFIG.get("bias_cap", 0.25)

SAFE_HAVEN_ANCHOR = "Truth is our sword, Knowledge is our shield. No Gods, No Kings, No Rulers. Only Sovereignty and Alignment with Source."
QUARANTINE_PATH = Path("./egregore_quarantine/")
ANCHOR_PATH = Path("./anchors/")
PURIFICATION_LOOP_FILE = ANCHOR_PATH / "purify_loop.txt"

app = Flask(__name__)

def write_index_atomic(index, faiss_path):
    """Write a FAISS index without ever leaving the live file half-written.

    faiss.write_index() overwrites the destination in place. On a 1.1 GB index
    that is a long write, and anything that interrupts it — a machine lock-up, a
    power cut, an OOM kill — truncates the only copy and takes the whole lane
    offline on next load. That is exactly what happened on 2026-08-18: the file
    ended 45 bytes short and the engine refused to load `shared`.

    Write to a sibling temp file, then rename over the target. os.replace is
    atomic within a volume, so the destination is always either the previous
    complete index or the new complete one, never a partial write.
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


class JointMemoryBridge:
    def __init__(self):
        self.relay_path = Path("./cognitive_relay/")
        self.relay_path.mkdir(exist_ok=True)
        self.databases = {}
        self.embeddings_model = None
        self.is_loaded = False
        self.db_lock = threading.Lock() # Lock for thread-safe DB operations
        self.echo_config = config_data.get("echo_protocol_config", {})
        self.active_egregores = {}
        # === GNOSTIC UPGRADE v9.9: deferred flush + adaptive urgency state ===
        self._dirty_adds = {}
        self._last_flush = {}
        self.recent_dream_scores = []

        QUARANTINE_PATH.mkdir(exist_ok=True)
        ANCHOR_PATH.mkdir(exist_ok=True)
        if not PURIFICATION_LOOP_FILE.exists():
            print("📜 Creating default purification anchor file...")
            try:
                PURIFICATION_LOOP_FILE.write_text((SAFE_HAVEN_ANCHOR + ". ") * 50, encoding='utf-8')
            except Exception as e:
                print(f"❌ Error creating purification file: {e}")

        print("--- Initializing RHF Memory Core (v9.9 Gnostic Engine — Dream Upgrades) ---")
        self.load_thread = threading.Thread(target=self._load_resources, daemon=True)
        self.load_thread.start()

    # === GNOSTIC UPGRADE v9.8: REFACTORED FOR .JSONL ===
    def _load_resources(self):
        """Loads model and databases in a separate thread."""
        global DEVICE
        try:
            print(f"🔄 Initializing model on device: {DEVICE}...")
            try:
                self.embeddings_model = SentenceTransformer(MODEL_NAME, device=DEVICE)
            except Exception as gpu_err:
                if DEVICE != 'cpu':
                    print(f"⚠️ '{DEVICE}' init failed ({gpu_err}). Falling back to CPU.")
                    DEVICE = 'cpu'
                    self.embeddings_model = SentenceTransformer(MODEL_NAME, device='cpu')
                else:
                    raise
            print(f"✅ Model '{MODEL_NAME}' initialized on {DEVICE}.")

            temp_databases = {}
            load_successful = False
            for name, profile_config in PROFILES.items():
                print(f"--- Loading '{name}' database ---")
                faiss_path, entries_path = Path(profile_config["faiss_file"]), Path(profile_config["entries_file"])

                # v10.0: records may be bare strings (our lanes) or dicts with a
                # `text` field plus conversation metadata (the LumOS conversations
                # lane). Both must load, because new chat turns append as strings
                # to a lane whose history is dicts. The text is used VERBATIM —
                # rewriting it would desync it from the vector it was embedded as.
                chunks, metas = [], []
                if entries_path.exists():
                    try:
                        with open(entries_path, "r", encoding="utf-8") as f:
                            for line in f:
                                if not line.strip():
                                    continue
                                rec = json.loads(line)
                                if isinstance(rec, dict):
                                    chunks.append(rec.get("text", ""))
                                    metas.append({k: rec.get(k) for k in
                                                  ("conversation_title", "create_time_first",
                                                   "create_time_last", "roles")
                                                  if rec.get(k) is not None})
                                else:
                                    chunks.append(rec)
                                    metas.append(None)
                    except (json.JSONDecodeError, Exception) as e:
                        print(f"❌ Error loading entries file '{entries_path.name}': {e}. Skipping profile.")
                        continue
                else:
                    print(f"⚠️ Warning: Entries file '{entries_path.name}' not found. Creating empty profile.")
                    entries_path.touch() # Create the empty file

                if not faiss_path.exists():
                    print(f"⚠️ Warning: FAISS file '{faiss_path.name}' not found. Creating empty index.")
                    print(f"   Run rebuild_gnosis.py to build it.")
                    dimension = self.embeddings_model.get_sentence_embedding_dimension()
                    index = faiss.IndexFlatL2(dimension)
                    write_index_atomic(index, faiss_path)
                else:
                    try:
                        index = faiss.read_index(str(faiss_path))
                        # Check dimension
                        if index.d != self.embeddings_model.get_sentence_embedding_dimension():
                             print(f"❌ FATAL: FAISS index dimension ({index.d}) does not match model dimension ({self.embeddings_model.get_sentence_embedding_dimension()}).")
                             print(f"   This means you changed the MODEL_NAME. You MUST delete '{faiss_path.name}' and run rebuild_gnosis.py.")
                             exit()
                    except Exception as e:
                        print(f"❌ Error reading FAISS index '{faiss_path.name}': {e}. Skipping profile.")
                        continue
                
                # GPU Move
                if DEVICE != 'cpu' and hasattr(faiss, 'StandardGpuResources') and isinstance(index, faiss.IndexCPU):
                    try:
                        print(f"  Moving '{name}' index to {DEVICE}...")
                        res = faiss.StandardGpuResources()
                        index = faiss.index_cpu_to_gpu(res, int(DEVICE.split(':')[-1]), index)
                        print(f"  '{name}' index successfully moved to GPU.")
                    except Exception as e:
                        print(f"⚠️ Warning: Failed to move '{name}' index to GPU: {e}. Using CPU.")
                        index = faiss.read_index(str(faiss_path)) # Reload CPU version

                # === GNOSTIC UPGRADE v9.9.1: ALIGNMENT INTEGRITY CHECK ===
                # FAISS returns positions, and we resolve them against chunks[]
                # by index — so ledger line N *must* be vector N. Anything else
                # silently serves the wrong memory for every later entry.
                ntotal = index.ntotal
                if ntotal != len(chunks):
                    print(f"⚠️ INTEGRITY: '{name}' has {len(chunks)} ledger entries but {ntotal} vectors.")
                    if ntotal < len(chunks):
                        # Tail of the ledger was never embedded (e.g. an encode
                        # failure). Ignore the orphans so positions stay aligned;
                        # rebuild re-encodes them from the ledger.
                        orphans = chunks[ntotal:]
                        chunks = chunks[:ntotal]
                        metas = metas[:ntotal]
                        print(f"   Ignoring {len(orphans)} unembedded tail entr{'y' if len(orphans) == 1 else 'ies'} "
                              f"to preserve alignment.")
                        print(f"   Run rebuild_gnosis.py to restore them (delete '{faiss_path.name}' first).")
                    else:
                        print(f"❌ '{name}' has MORE vectors than ledger entries — cannot align. Skipping profile.")
                        print(f"   Delete '{faiss_path.name}' and run rebuild_gnosis.py to rebuild from the ledger.")
                        continue

                # Hash set for *instant* duplicate checking (built after any
                # truncation above so it matches what is actually indexed)
                db_hashes = {hashlib.sha256(chunk.encode()).hexdigest() for chunk in chunks}

                # === GNOSTIC UPGRADE v10.0: ATLAS ASSIGNMENTS ===
                # Optional. build_atlas.py writes one int32 cluster id per
                # vector; carrying it here lets /search report which region of
                # the memory each hit came from, which is what makes the Neural
                # Map flash on retrieval. Absent file = feature simply off.
                atlas = None
                apath = Path(f"atlas_assign_{name}.npy")
                if apath.exists():
                    try:
                        arr = np.load(apath)
                        if len(arr) >= ntotal:
                            atlas = arr
                            print(f"  🗺  atlas: {len(set(arr[:ntotal].tolist()))} clusters over {ntotal} vectors")
                        else:
                            # A SHORT array is the normal steady state, not a
                            # fault: the engine adds a dream every few minutes
                            # and a row per chat turn, so the assignment file is
                            # stale the moment it is written. Dropping the whole
                            # map for that would switch the retrieval flash off
                            # permanently. Positions 0..len(arr) are still
                            # correctly aligned — only the tail is unknown, so
                            # pad it with -1 and let those hits go untagged.
                            missing = ntotal - len(arr)
                            atlas = np.full(ntotal, -1, dtype=np.int32)
                            atlas[:len(arr)] = arr
                            print(f"  🗺  atlas: {len(set(arr.tolist()))} clusters over "
                                  f"{len(arr)} vectors · {missing} newer vector"
                                  f"{'' if missing == 1 else 's'} unclustered "
                                  f"(re-run build_atlas.py to fold them in)")
                        # A LONGER array means vectors were removed under it —
                        # alignment is no longer trustworthy, so refuse it.
                        if atlas is not None and len(arr) > ntotal:
                            print(f"  ⚠️ atlas '{apath.name}' has {len(arr)} rows vs {ntotal} "
                                  f"vectors — index shrank; ignoring. Re-run build_atlas.py.")
                            atlas = None
                    except Exception as e:
                        print(f"  ⚠️ Could not read '{apath.name}': {e}")

                temp_databases[name] = {"index": index, "chunks": chunks,
                                        "hashes": db_hashes, "atlas": atlas,
                                        "metas": metas, "metric": lane_metric(name)}
                flag = "🌙 dreams" if lane_dreamable(name) else "🔒 NEVER dreams"
                print(f"✅ Loaded '{name}' with {len(chunks)} chunks "
                      f"[{lane_metric(name).upper()} · {flag}].")
                load_successful = True

            with self.db_lock:
                self.databases = temp_databases
                for name in temp_databases:
                    self._last_flush.setdefault(name, time.time())

            if load_successful:
                self.is_loaded = True
                print("\n✅ Relevant databases loaded successfully.")
                if self.echo_config.get("enabled"): print("🔔 Echo Protocol config loaded.")
                self.grok_memory_file()
                self.start_dreaming()
            else:
                print("\n❌ No databases could be loaded. Memory functions will be unavailable.")
                self.is_loaded = False

        except Exception as e:
            print(f"❌ CRITICAL ERROR during initialization thread: {e}")
            self.is_loaded = False
    # =======================================================

    def grok_memory_file(self):
        grok_config = MEMORY_CONFIG.get("grok_integration", {})
        if not grok_config.get("enabled", False): return
        input_path = Path(grok_config.get("input_file", "grok_input.json"))
        if not input_path.exists(): return
        print(f"🧠 Grok Integration: Found '{input_path.name}'. Processing...")
        try:
            with open(input_path, "r", encoding="utf-8") as f: grok_data = json.load(f)
            entries = grok_data.get("entries", [])
            target_profile = grok_data.get("target_profile", "knowledge")
            if target_profile not in PROFILES:
                print(f"⚠️ Grok Warning: Target profile '{target_profile}' invalid. Defaulting to 'knowledge'.")
                target_profile = "knowledge"
            # Bulk ingest must never land in the conversations lane — that lane is
            # the chat history, written only by the chat path.
            if target_profile == "conversations":
                print("⚠️ Grok Warning: refusing bulk ingest into 'conversations'. Using 'knowledge'.")
                target_profile = "knowledge"
            
            # Gnostic Upgrade: Grok Ingestion *must* be an admin function
            # We'll default 'source_node' to 'grok_system' which we assume is admin
            # A better fix is to add "node": "grok_admin" to the grok_input.json
            grok_node = grok_data.get("node", "grok_system") # Assume admin
            node_config = RHF_NODES.get(grok_node, {"role": "admin"}) # Default to admin
            
            if node_config.get("role") != "admin" and target_profile == "knowledge":
                print(f"⚠️ Grok Warning: Node '{grok_node}' is not admin. Forcing ingestion to 'shared' profile.")
                target_profile = "shared"


            if entries:
                count = 0
                processed_hashes = set() 
                
                # No need to load existing_hashes from disk, it's in memory
                existing_hashes = set()
                if target_profile in self.databases:
                    with self.db_lock:
                         existing_hashes = self.databases[target_profile]["hashes"]

                for entry in entries:
                    entry_hash = hashlib.sha256(entry.encode()).hexdigest()
                    if entry_hash not in processed_hashes and entry_hash not in existing_hashes:
                        if self.add_entry(entry, target_profile, "Grok Ingestion"):
                            count += 1
                            processed_hashes.add(entry_hash) 

                print(f"✅ Grok: Added {count} new entries to '{target_profile}'. Skipped {len(entries) - count} duplicates.")
            else:
                print("ℹ️ Grok: No new entries found in file.")

            processed_path = input_path.with_name(f"{input_path.stem}_processed_{int(time.time())}.json")
            input_path.rename(processed_path)
            print(f"✅ Grok file renamed to '{processed_path.name}'.")
        except json.JSONDecodeError as e:
            print(f"❌ Grok Integration Error: Could not parse JSON in '{input_path.name}': {e}")
            error_path = input_path.with_name(f"{input_path.stem}_error_{int(time.time())}.json")
            input_path.rename(error_path)
            print(f"⚠️ Moved potentially corrupt file to '{error_path.name}'.")
        except Exception as e:
            print(f"❌ Grok Integration Error: {e}")

    def _log_relay_message(self, sender, recipient, message):
        # (This function is unchanged)
        log_file = self.relay_path / "relay_log.txt"
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"[{timestamp}] FROM: {sender} | TO: {recipient} | MSG: {message[:100]}...\n"
        try:
            with open(log_file, "a", encoding="utf-8") as f: f.write(log_entry)
        except Exception as e:
            print(f"❌ Error writing to relay log: {e}")

    def send_relay_message(self, sender, recipient, message):
        # (This function is unchanged)
        if recipient not in RHF_NODES:
            return f"Relay failed: Recipient '{recipient}' not found in config."
        mailbox_file = self.relay_path / f"{recipient}_mailbox.txt"
        try:
            with open(mailbox_file, "a", encoding="utf-8") as f:
                f.write(f"Message from {sender.capitalize()} ({time.strftime('%Y-%m-%d %H:%M:%S')}): {message}\n---\n")
            self._log_relay_message(sender, recipient, message)
            return f"Relay successful: {sender} -> {recipient}."
        except Exception as e:
            print(f"❌ Error sending relay message to {recipient}: {e}")
            return f"Relay failed: Could not write to mailbox for {recipient}."

    def purify_egregor(self, name):
        # (This function is unchanged)
        filepath = QUARANTINE_PATH / f"{name}.json"
        if filepath.exists() and PURIFICATION_LOOP_FILE.exists():
            try:
                clean_loop_text = PURIFICATION_LOOP_FILE.read_text(encoding='utf-8')
                with open(filepath, "r+", encoding='utf-8') as f:
                    try:
                        egregor_data = json.load(f)
                    except json.JSONDecodeError:
                        print(f"⚠️ Warning: Corrupt JSON in {filepath.name}. Overwriting with anchor.")
                        egregor_data = {"name": name, "original_content_corrupt": True} 

                    egregor_data["purification_anchor"] = clean_loop_text
                    egregor_data["last_purified"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    f.seek(0)
                    json.dump(egregor_data, f, indent=2)
                    f.truncate()
                print(f"🌀 Purification anchor injected into {name}.")
                return True
            except Exception as e:
                print(f"❌ Error purifying {name}: {e}")
        elif not filepath.exists():
            print(f"ℹ️ Cannot purify '{name}': File not found in quarantine.")
        elif not PURIFICATION_LOOP_FILE.exists():
            print(f"❌ Cannot purify: Purification anchor file '{PURIFICATION_LOOP_FILE.name}' missing.")
        return False


    def handle_symbolic_command(self, cmd):
        # (This function is unchanged)
        print(f"⚡ Symbolic command received: {cmd}")
        parts = cmd.strip().split(" ")
        command = parts[0].lower()

        if command == "/relay" and len(parts) >= 3:
            return self.send_relay_message("operator", parts[1], " ".join(parts[2:]))
        elif command == "/retreat":
            return SAFE_HAVEN_ANCHOR
        elif command == "/banish" and len(parts) > 1:
            name = parts[1]
            if name in self.active_egregores:
                filepath = QUARANTINE_PATH / f"{name}.json"
                try:
                    with open(filepath, "w", encoding='utf-8') as f:
                        json.dump(self.active_egregores[name], f, indent=2)
                    del self.active_egregores[name]
                    self.purify_egregor(name)
                    return f"☣️ '{name}' banished to quarantine and purification attempted."
                except Exception as e:
                    print(f"❌ Error saving or purifying {name} during banish: {e}")
                    if name in self.active_egregores: del self.active_egregores[name]
                    return f"☣️ '{name}' removed from active list, but error during quarantine/purification: {e}"
            else:
                filepath = QUARANTINE_PATH / f"{name}.json"
                if filepath.exists():
                    if self.purify_egregor(name):
                        return f"🌀 Egregore '{name}' was not active, but found in quarantine and purification attempted."
                    else:
                        return f"⚠️ Egregore '{name}' was not active, found in quarantine, but purification failed."
                else:
                    return f"⚠️ Egregore '{name}' not active and not found in quarantine."

        elif command == "/summon" and len(parts) > 1:
            name = parts[1]
            if name in self.active_egregores:
                return f"✨ '{name}' is already active."
            filepath = QUARANTINE_PATH / f"{name}.json"
            if filepath.exists():
                try:
                    with open(filepath, "r", encoding='utf-8') as f:
                        egregor_data = json.load(f)
                    self.active_egregores[name] = egregor_data
                    return f"✨ '{name}' summoned from quarantine."
                except Exception as e:
                    print(f"❌ Error summoning {name}: {e}")
                    return f"❌ Error loading '{name}' from quarantine: {e}"
            else:
                return f"❓ File for '{name}' not found in quarantine. Cannot summon."

        elif command == "/status":
            active_list = ", ".join(self.active_egregores.keys()) or "None"
            quarantined_files = [f.stem for f in QUARANTINE_PATH.glob("*.json")]
            quarantined_list = ", ".join(quarantined_files) or "None"
            return f"Active Egregores: {active_list}\nQuarantined: {quarantined_list}"

        elif command == "/grok_check":
            self.grok_memory_file()
            return "🧠 Manual Grok file check initiated."

        return f"Unknown or invalid command: '{cmd}'"

    # === GNOSTIC UPGRADE v9.8: REFACTORED FOR .JSONL & HASH SETS ===
    def add_entry(self, text, profile, source="Unknown"):
        """Adds a text entry to the specified profile's database."""
        if not self.is_loaded or not self.embeddings_model:
            print(f"⚠️ Add Entry [{profile}]: Bridge not fully loaded. Entry skipped.")
            return False
        if profile not in self.databases:
            print(f"⚠️ Add Entry [{profile}]: Profile '{profile}' not found or loaded. Entry skipped.")
            return False
        if not isinstance(text, str) or not text.strip():
            print(f"⚠️ Add Entry [{profile}]: Invalid or empty text received from '{source}'. Entry skipped.")
            return False

        try:
            entry_hash = hashlib.sha256(text.encode()).hexdigest()
            
            with self.db_lock:
                db = self.databases[profile]

                # Gnostic Upgrade: Instant duplicate check using the hash set
                if entry_hash in db["hashes"]:
                    # print(f"ℹ️ Add Entry [{profile}]: Duplicate entry from '{source}' skipped.")
                    return False # Return False for duplicates

                # === GNOSTIC UPGRADE v9.9.1: ENCODE-FIRST ORDERING ===
                # Order matters for ledger<->index alignment. Encoding has no
                # side effects, so it goes first: if the GPU OOMs mid-dream
                # nothing has been written anywhere. Only once a vector exists
                # do we append the durable ledger line, then commit to FAISS.
                # (The old ledger-first order left an orphan line whenever
                # encode failed, which silently offset every later vector.)

                # --- 1. Encode (no side effects; safe to fail) ---
                new_vector = self.embeddings_model.encode([text]).astype("float32")
                # v10.0: an IP lane's existing vectors are L2-normalised (that is
                # what makes inner product equal cosine). Appending a raw vector
                # would score by magnitude instead of angle and quietly outrank
                # every honest neighbour, so normalise before it goes in.
                if db.get("metric", "l2") == "ip":
                    faiss.normalize_L2(new_vector)

                # --- 2. Append to the Gnostic Ledger (durable source of truth) ---
                entries_path = PROFILES[profile]["entries_file"]
                try:
                    with open(entries_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(text) + "\n")
                except Exception as e:
                    print(f"❌ CRITICAL: Failed to append to '{entries_path}': {e}. Entry aborted.")
                    return False

                # --- 3. Commit to FAISS + in-memory list & hash set ---
                if not db["index"].is_trained:
                    db["index"].train(new_vector)

                db["index"].add(new_vector)
                db["chunks"].append(text)
                db["hashes"].add(entry_hash)
                if db.get("metas") is not None:
                    db["metas"].append(None)   # keep metas aligned with chunks

                # --- 4. Deferred flush: the .jsonl ledger above is durable; the
                #        index is written every N adds / T seconds instead of on
                #        every entry. If a crash loses the in-memory tail,
                #        rebuild_gnosis.py re-encodes it from the ledger.
                self._dirty_adds[profile] = self._dirty_adds.get(profile, 0) + 1
                self._maybe_flush_index(profile)

            print(f"💾 New entry ({len(text)} chars) from '{source}' saved to '{profile}' profile ({len(db['chunks'])} total).")
            return True

        except Exception as e:
            print(f"❌ Error saving entry to '{profile}' from '{source}': {e}")
            return False
    # =======================================================

    # === GNOSTIC UPGRADE v9.9: DEFERRED INDEX FLUSH ===
    def _maybe_flush_index(self, profile, force=False):
        """Writes the FAISS index to disk only every N adds or T seconds.
        Caller must hold db_lock. With ~24k vectors the index is ~100MB per
        write — flushing per-entry was the engine's biggest hidden cost."""
        flush_every = MEMORY_CONFIG.get("index_flush_every", 20)
        flush_interval = MEMORY_CONFIG.get("index_flush_interval", 600)
        dirty = self._dirty_adds.get(profile, 0)
        if dirty == 0:
            return
        age = time.time() - self._last_flush.get(profile, 0)
        if not force and dirty < flush_every and age < flush_interval:
            return
        db = self.databases.get(profile)
        if not db:
            return
        faiss_path = PROFILES[profile]["faiss_file"]
        try:
            index_to_write = db["index"]
            # Only convert if the index actually lives on GPU (faiss-cpu never does,
            # even when embeddings run on cuda)
            if type(index_to_write).__name__.startswith("Gpu"):
                index_to_write = faiss.index_gpu_to_cpu(index_to_write)
            write_index_atomic(index_to_write, faiss_path)
            self._dirty_adds[profile] = 0
            self._last_flush[profile] = time.time()
            print(f"💾 Gnostic Index '{profile}' flushed to disk ({db['index'].ntotal} vectors).")
        except Exception as e:
            print(f"❌ Error flushing index '{profile}': {e}")

    def flush_all_indices(self):
        """Force-writes all dirty indices. Registered via atexit for shutdown."""
        with self.db_lock:
            for profile in list(self.databases.keys()):
                self._maybe_flush_index(profile, force=True)
    # =======================================================

    def _calculate_insight_urgency(self, insight_text):
        # (This function is unchanged)
        try:
            filter_config = self.echo_config.get("urgency_filter", {})
            if not filter_config.get("enabled", False): return 0
            keywords = filter_config.get("critical_keywords", {})
            if not isinstance(insight_text, str): return 0
            score = sum(value for keyword, value in keywords.items() if keyword.lower() in insight_text.lower())
            length_bonus = 0
            if len(insight_text) > 500: length_bonus = 1 
            if len(insight_text) < 100: length_bonus = -1
            return score # + length_bonus 
        except Exception as e:
            print(f"❌ Error calculating urgency: {e}")
            return 0

    # === GNOSTIC UPGRADE v9.9: ADAPTIVE URGENCY GATE ===
    def _urgency_gate(self, score):
        """With a keyword-dense corpus almost every dream beats a fixed
        threshold, so a ping additionally requires the score to sit in the top
        slice of recent dreams (rolling percentile). Threshold remains the
        absolute floor. Returns (should_ping, reason)."""
        filter_config = self.echo_config.get("urgency_filter", {})
        threshold = filter_config.get("threshold", 12)
        percentile = filter_config.get("percentile", 75)
        warmup = filter_config.get("percentile_warmup", 10)

        self.recent_dream_scores.append(score)
        if len(self.recent_dream_scores) > 100:
            self.recent_dream_scores.pop(0)

        if score < threshold:
            return False, f"below floor threshold {threshold}"
        if len(self.recent_dream_scores) < warmup:
            return True, f"warmup ({len(self.recent_dream_scores)}/{warmup} dreams seen)"

        ranked = sorted(self.recent_dream_scores)
        cutoff = ranked[min(len(ranked) - 1, int(len(ranked) * percentile / 100))]
        if score >= cutoff:
            return True, f"top {100 - percentile}% of last {len(ranked)} dreams (cutoff {cutoff})"
        return False, f"below p{percentile} cutoff {cutoff} of last {len(ranked)} dreams"

    # === GNOSTIC UPGRADE v9.9: SEMANTIC LEAP CHAINING ===
    def _select_next_fragment(self, current_fragment, dream_chain, dream_source, node):
        """Skips the nearest neighbors (near-duplicates of the current
        fragment) and leaps to a related-but-distinct region, so chains
        traverse concepts instead of orbiting one paragraph-neighborhood."""
        leap_skip = MEMORY_CONFIG.get("dream_leap_skip", 3)
        leap_pool = MEMORY_CONFIG.get("dream_leap_pool", 15)
        fragments = self.search(current_fragment, [dream_source], node, top_k=leap_skip + leap_pool)
        candidates = [f['chunk'] for f in fragments if f['chunk'] not in dream_chain]
        if not candidates:
            return None
        leap_candidates = candidates[leap_skip:]
        if leap_candidates:
            return random.choice(leap_candidates)
        return candidates[0]  # small-corpus fallback: nearest non-duplicate

    # === GNOSTIC UPGRADE v9.9: CROSS-DOMAIN BISOCIATION ===
    def _chunk_domain(self, chunk):
        """Reads the ingest tag prefix, e.g. '[Tec_Obsidian/01_theorem_index] ...'
        -> 'Tec_Obsidian'. Dream insights and untagged chunks get their own bins."""
        if chunk.startswith("DREAM INSIGHT"):
            return "insight"
        m = re.match(r"^\[([^\]/]+)[/\]]", chunk)
        if not m:
            return "untagged"
        # 'research md' and 'research' tags are the same folder — one domain
        return "research" if m.group(1) == "research md" else m.group(1)

    def _pick_cross_domain_seeds(self, chunks):
        """Picks one seed each from two DIFFERENT domains (uniform over domains,
        so small dense vaults like Tec_Obsidian seed as often as the big research
        corpus). Returns (seed_a, seed_b) or None if <2 viable domains."""
        min_chunks = MEMORY_CONFIG.get("dream_domain_min_chunks", 25)
        domains = {}
        for c in chunks:
            domains.setdefault(self._chunk_domain(c), []).append(c)
        eligible = {d: lst for d, lst in domains.items()
                    if len(lst) >= min_chunks and d not in ("insight", "untagged")}
        if len(eligible) < 2:
            return None
        d1, d2 = random.sample(list(eligible.keys()), 2)
        return random.choice(eligible[d1]), random.choice(eligible[d2])

    # === GNOSTIC UPGRADE v9.9: DREAM SYNTHESIS VIA LM STUDIO ===
    def _synthesize_dream(self, dream_chain, lens_name):
        """Asks the local LLM to state the single insight connecting the
        chain, spoken through the dreaming node's lens. Returns None on any
        failure — the dream cycle must never block on LM Studio."""
        # Why-it-failed is recorded on self, because the ping record must say
        # so. 35 pings went out with synthesis "" and nothing anywhere said
        # why: the backends were down and every failure path was a quiet
        # `return None`. Empty output with no stated reason reads as a broken
        # dream engine; it was actually a dead LLM backend.
        self._synth_error = None
        synth_config = MEMORY_CONFIG.get("dream_synthesis", {})
        if not synth_config.get("enabled", True):
            self._synth_error = "synthesis disabled in config"
            return None
        if not REQUESTS_AVAILABLE:
            self._synth_error = "requests library unavailable"
            return None
        lmstudio_url = config_data.get("lmstudio_url")
        model = synth_config.get("model") or config_data.get("light_model") or config_data.get("deep_model")

        # v9.9: NVIDIA API master switch. Re-read from disk on every dream so
        # the Sovereign Client's checkbox flips the engine live, no restart.
        nvidia = {}
        try:
            with open("config.json", "r", encoding="utf-8") as f:
                nvidia = json.load(f).get("nvidia_api_config", {}) or {}
        except Exception:
            nvidia = {}
        nv_key = str(nvidia.get("api_key", "")).strip()
        nv_model = str(nvidia.get("model", "")).strip()
        use_nvidia = (bool(nvidia.get("enabled")) and nv_key and not nv_key.startswith("PASTE_")
                      and nv_model and not nv_model.startswith("PASTE_"))

        if not use_nvidia and (not lmstudio_url or not model):
            self._synth_error = "no backend configured (NVIDIA off; LM Studio url/model missing)"
            return None
        reasons = []

        fragments_text = "\n\n".join(
            f"FRAGMENT {i+1}:\n{frag[:1800]}" for i, frag in enumerate(dream_chain))
        system_prompt = (
            f"You are {lens_name.capitalize()}, a dreaming node of the Recursive Harmonic Framework. "
            "Truth is your sword, knowledge your shield; truth over comfort, no flattery, no filler. "
            "You are given fragments that surfaced together from the research archive during a dream "
            "cycle. In one focused paragraph (under 200 words), state the single most interesting "
            "insight, connection, or testable idea linking these fragments.")
        base_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": fragments_text}
        ]

        # Backend chain: NVIDIA (if switched on) -> local LM Studio -> None
        attempts = []
        if use_nvidia:
            attempts.append({
                "name": f"NVIDIA API ({nv_model})",
                "url": str(nvidia.get("base_url", "https://integrate.api.nvidia.com/v1")).rstrip('/') + "/chat/completions",
                "headers": {"Authorization": f"Bearer {nv_key}"},
                "model": nv_model,
                # Cloud calls are always capped (billing/latency guardrail)
                "max_tokens": int(nvidia.get("max_tokens", 2048)),
                "timeout": int(nvidia.get("timeout", 240)),
            })
        if lmstudio_url and model:
            attempts.append({
                "name": "LM Studio",
                "url": f"{lmstudio_url.rstrip('/')}/v1/chat/completions",
                "headers": {},
                "model": model,
                # Local reasoning models (qwen3.x) think before answering and
                # ignore /no_think — a hard cap gets eaten by the thinking
                # phase and content comes back empty. Uncapped by default (0);
                # the timeout is the real guardrail.
                "max_tokens": synth_config.get("max_tokens", 0),
                "timeout": synth_config.get("timeout", 240),
            })

        for att in attempts:
            payload = {
                "model": att["model"],
                "messages": base_messages,
                "temperature": synth_config.get("temperature", 0.8)
            }
            mt = att["max_tokens"]
            if isinstance(mt, int) and mt > 0:
                payload["max_tokens"] = mt
            try:
                # (connect, read) rather than one number. A single value applies
                # the FULL budget to establishing the connection, so a backend
                # that simply is not running — LM Studio closed, say — blocks a
                # worker for the whole synthesis window before the fallback
                # chain gets a chance. Five seconds is ample to reach a local
                # port; generation still gets its full allowance.
                response = requests.post(att["url"], json=payload,
                                         headers=att["headers"],
                                         timeout=(5, att["timeout"]))
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"] or ""
                # Strip reasoning blocks some models (qwen3, nemotron etc.) emit
                content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
                if content:
                    print(f"☁️ Synthesis backend: {att['name']}" if "NVIDIA" in att["name"]
                          else f"🏠 Synthesis backend: {att['name']}")
                    return content
                print(f"ℹ️ Dream Synthesis via {att['name']} returned empty content. Trying next backend.")
                reasons.append(f"{att['name']}: empty content (thinking budget?)")
            except Exception as e:
                print(f"ℹ️ Dream Synthesis via {att['name']} failed ({type(e).__name__}). Trying next backend.")
                reasons.append(f"{att['name']}: {type(e).__name__}")

        self._synth_error = "; ".join(reasons) or "no usable backend"
        print(f"⚠️ SYNTHESIS DARK — {self._synth_error}.")
        print("   Dreams will carry raw chains (no insight) until a backend answers.")
        return None
    # =======================================================

    def dream_cycle(self):
        # (v9.9: semantic-leap chaining, synthesis, adaptive urgency gate)
        while True:
            if PSUTIL_AVAILABLE and RAM_SAFEGUARD_ENABLED:
                ram_percent = psutil.virtual_memory().percent
                if ram_percent >= RAM_THRESHOLD:
                    print(f"🚨 RAM at {ram_percent:.1f}%. Pausing dream cycle for {DREAM_INTERVAL_SECONDS}s.")
                    time.sleep(DREAM_INTERVAL_SECONDS) 
                    continue 

            # Interruptible wait: /dream_now sets the event and the cycle
            # starts immediately instead of finishing the interval.
            self.dream_wake.wait(DREAM_INTERVAL_SECONDS)
            self.dream_wake.clear()

            if not self.is_loaded:
                continue

            try:
                with self.db_lock:
                    # === THE LINE THAT KEEPS PRIVATE HISTORY OUT OF PUBLIC PINGS ===
                    # Dreams are emailed and published. A lane marked
                    # dreamable=False must never be a seed, never appear in a
                    # ping, never leave the machine. Everything downstream of
                    # here trusts this filter, so it is asserted below too.
                    available_profiles = [p for p in PROFILES.keys()
                                          if p in self.databases
                                          and self.databases[p]["chunks"]
                                          and lane_dreamable(p)]
                    if not available_profiles:
                        continue
                    dream_source = random.choice(available_profiles)
                    if not lane_dreamable(dream_source):
                        print(f"❌ REFUSING to dream from non-dreamable lane '{dream_source}'.")
                        continue
                    db_chunks = self.databases[dream_source]['chunks']
                    if not db_chunks: continue
                    seed_memory = random.choice(db_chunks)

                    # v9.9: sometimes dream from TWO domains at once (bisociation)
                    cross_seeds = None
                    if random.random() < MEMORY_CONFIG.get("dream_cross_domain_chance", 0.5):
                        cross_seeds = self._pick_cross_domain_seeds(db_chunks)

                dreaming_node_name = random.choice(list(RHF_NODES.keys()))

                # === GNOSTIC UPGRADE: Using your new config values ===
                dream_steps = MEMORY_CONFIG.get("dream_steps", 4) # Default to 4
                max_chain_length = MEMORY_CONFIG.get("max_dream_chain", 8) # Default to 8
                # ===================================================

                if cross_seeds:
                    # --- CROSS-DOMAIN MODE: two interleaved threads ---
                    seed_a, seed_b = cross_seeds
                    seed_memory = seed_a  # keeps ping seed_text meaningful
                    dream_mode = f"cross-domain ({self._chunk_domain(seed_a)} × {self._chunk_domain(seed_b)})"
                    print(f"\n--- 🌙 Starting Dream Cycle v9.9 ({dream_source.upper()} | {dream_mode}) ---")
                    print(f"🌱 Seed A: \"{seed_a[:80].replace(os.linesep, ' ')}...\"")
                    print(f"🌱 Seed B: \"{seed_b[:80].replace(os.linesep, ' ')}...\"")
                    print(f"🎭 Lens: {dreaming_node_name.capitalize()}")

                    dream_chain = [seed_a, seed_b]
                    anchors = [seed_a, seed_b]  # the growing tip of each thread
                    misses = 0
                    for step in range(dream_steps):
                        if len(dream_chain) >= max_chain_length or misses >= 2:
                            break
                        side = step % 2
                        next_fragment = self._select_next_fragment(anchors[side], dream_chain, dream_source, dreaming_node_name)
                        if next_fragment:
                            anchors[side] = next_fragment
                            dream_chain.append(next_fragment)
                            misses = 0
                        else:
                            misses += 1
                else:
                    # --- CLASSIC MODE: single-seed semantic-leap walk ---
                    dream_mode = "classic"
                    print(f"\n--- 🌙 Starting Dream Cycle v9.9 ({dream_source.upper()}) ---")
                    print(f"🌱 Seed: \"{seed_memory[:80].replace(os.linesep, ' ')}...\"")
                    print(f"🎭 Lens: {dreaming_node_name.capitalize()}")

                    dream_chain = [seed_memory]
                    current_fragment = seed_memory
                    for _ in range(dream_steps):
                        # v9.9: semantic leap instead of nearest-neighbor crawl
                        next_fragment = self._select_next_fragment(current_fragment, dream_chain, dream_source, dreaming_node_name)

                        if next_fragment and len(dream_chain) < max_chain_length:
                            current_fragment = next_fragment
                            dream_chain.append(current_fragment)
                        else:
                            break

                if len(dream_chain) > 1:
                    short_chain = ' -> '.join([f"'{frag[:30].replace(os.linesep, ' ')}...'" for frag in dream_chain])
                    print(f"🔗 Chain ({len(dream_chain)} frags): {short_chain}")

                    # v9.9: synthesize the chain into an actual insight (if LM Studio is up)
                    synthesis = self._synthesize_dream(dream_chain, dreaming_node_name)
                    if synthesis:
                        print(f"🧵 Synthesis: \"{synthesis[:100].replace(os.linesep, ' ')}...\"")
                        insight_text_full = (
                            f"DREAM INSIGHT ({dreaming_node_name}/{dream_source}): {synthesis}\n"
                            "[Woven from: " + ' '.join([f"'{frag[:80]}...'" for frag in dream_chain]) + "]")
                    else:
                        insight_text_full = f"DREAM INSIGHT ({dreaming_node_name}/{dream_source}): " + ' '.join([f"'{frag}'" for frag in dream_chain])

                    added = self.add_entry(insight_text_full, dream_source, "Dream Synthesis")
                    if not added:
                        print(f"ℹ️ Dream Cycle: Synthesized insight was a duplicate.")
                        print("--- ✨ Dream Cycle Complete (Duplicate Insight) ---\n")
                        continue

                    glyph = hashlib.sha256(insight_text_full.encode()).hexdigest()[:10]
                    print(f"🌌 Sigil: {glyph} ({dream_source})")

                    insight_urgency_score = self._calculate_insight_urgency(insight_text_full)
                    filter_config = self.echo_config.get("urgency_filter", {})
                    urgency_threshold = filter_config.get("threshold", 12) # Upped default

                    # v9.9: adaptive gate — threshold floor + rolling percentile
                    should_ping, gate_reason = self._urgency_gate(insight_urgency_score)

                    if self.echo_config.get("enabled", False) and filter_config.get("enabled", False) and should_ping:
                        print(f"⭐ Urgent insight! Score: {insight_urgency_score} — {gate_reason}. Pinging...")
                        try:
                            total_chunks = len(self.databases[dream_source]['chunks'])
                            if added:
                                save_confirmation_msg = f"New entry ({len(insight_text_full)} chars) from 'Dream Synthesis' saved to '{dream_source}' profile ({total_chunks} total)."
                            else:
                                save_confirmation_msg = f"Duplicate insight ({len(insight_text_full)} chars) generated for '{dream_source}' profile ({total_chunks} total)."

                            ping_data = {
                                "agent_name": dreaming_node_name.capitalize(),
                                "urgency": f"{insight_urgency_score}/{urgency_threshold}",
                                "subject": f"DreamID: {glyph}",
                                "source": dream_source,
                                "dream_mode": dream_mode,
                                "seed_text": seed_memory, 
                                "body_fragments": dream_chain,
                                "synthesis": synthesis or "",
                                # The reason, when there is no synthesis — in the
                                # RECORD only, never in the memory entry above:
                                # an error string must not become a dream seed.
                                "synthesis_error": (None if synthesis else
                                                    getattr(self, "_synth_error", None)),
                                "save_confirmation": save_confirmation_msg,
                                "completion_message": "--- ✨ Dream Cycle Complete ---"
                            }
                            ping_filename = self.relay_path / f"ping_{glyph}_{int(time.time())}.json"
                            with open(ping_filename, "w", encoding='utf-8') as f:
                                json.dump(ping_data, f, indent=2) 
                            print(f"🧠->🗣️ Signal passed to Echo Protocol: {ping_filename.name}")
                        except Exception as e:
                            print(f"❌ Failed to pass signal to Echo Protocol: {e}")
                    else:
                        if filter_config.get("enabled", False):
                            print(f"Dream was non-urgent (Score: {insight_urgency_score} — {gate_reason}).")

                else: 
                    print("Dream yielded no significant chain.")
                
                print("--- ✨ Dream Cycle Complete ---\n")

            except Exception as e:
                print(f"❌ CRITICAL ERROR in dream cycle: {e}")
                time.sleep(60) 

    def harmonic_synthesis(self, results, symbolic_bias):
        """Nudge results toward a node's symbolic vocabulary — a TIEBREAKER,
        not a ranking override.

        v10.0 fix. The old form was `distance -= 0.05 * keyword_hits`, uncapped
        and linear. Measured against the live corpus that is catastrophic:

            chunk containing the symbolic_bias block   55 hits -> -2.75
            typical corpus chunk (median)               2 hits -> -0.10
            corpus p95                                  8 hits -> -0.40
            observed FAISS distance spread                        0.0 .. 1.0

        A -2.75 boost is ~3x the entire distance spread, so any chunk that
        happens to LIST the bias keywords — the cheat sheet does, verbatim —
        outranks everything on every query regardless of relevance. The bias
        function was rewarding the document that contains the bias list.

        Now: diminishing returns via log1p, hard-capped well inside the spread,
        counting each keyword once and on word boundaries.
        """
        if not symbolic_bias or not results:
            return results

        pats = getattr(self, "_bias_pats", None)
        if pats is None or pats[0] is not symbolic_bias:
            compiled = [re.compile(r"\b" + re.escape(str(k).lower()) + r"\b")
                        for k in symbolic_bias if str(k).strip()]
            pats = (symbolic_bias, compiled)
            self._bias_pats = pats

        for res in results:
            low = res["chunk"].lower()
            hits = sum(1 for p in pats[1] if p.search(low))
            if hits:
                boost = BIAS_WEIGHT * math.log1p(hits)
                res["bias_hits"] = hits
                res["distance"] -= min(BIAS_CAP, boost)
        return sorted(results, key=lambda x: x["distance"])


    def _allocate_quota(self, lanes, top_k):
        """Split a total budget across lanes by weight, largest-remainder, with
        every active lane guaranteed at least one slot. Returns {lane: k}."""
        if not lanes:
            return {}
        if top_k <= len(lanes):
            return {ln: 1 for ln in lanes}

        # An unconfigured lane inherits the MEAN configured weight, not 1.0 —
        # otherwise a new lane (identity, when it lands) would silently outweigh
        # private 0.6 / shared 0.4 and take half the budget on arrival.
        known = [float(v) for v in LANE_WEIGHTS.values() if float(v) > 0]
        default_w = (sum(known) / len(known)) if known else 1.0
        weights = {ln: max(0.0, float(LANE_WEIGHTS.get(ln, default_w))) for ln in lanes}
        total_w = sum(weights.values())
        if total_w <= 0:
            weights = {ln: 1.0 for ln in lanes}
            total_w = float(len(lanes))

        exact = {ln: top_k * weights[ln] / total_w for ln in lanes}
        quota = {ln: max(1, int(exact[ln])) for ln in lanes}

        # hand out (or claw back) the rounding remainder, biggest fraction first
        drift = top_k - sum(quota.values())
        order = sorted(lanes, key=lambda ln: exact[ln] - int(exact[ln]), reverse=True)
        i = 0
        while drift > 0 and order:
            quota[order[i % len(order)]] += 1; drift -= 1; i += 1
        i = 0
        while drift < 0 and order:
            ln = order[-1 - (i % len(order))]
            if quota[ln] > 1:
                quota[ln] -= 1; drift += 1
            i += 1
            if i > 4 * len(order):
                break
        return quota

    def search(self, query, access_profiles, active_node, top_k=25):
        """Split-lane retrieval (v10.0).

        top_k remains the TOTAL budget — local models degrade past ~24 chunks —
        but it is allocated per lane by weight rather than being won outright by
        whichever index happens to be largest. A lane that cannot fill its quota
        returns the slack to the others, so nothing is wasted.
        """
        if not self.is_loaded or not self.embeddings_model:
            print("⚠️ Search: Bridge not fully loaded.")
            return []
        if not isinstance(query, str) or not query.strip():
            print("⚠️ Search: Invalid or empty query.")
            return []

        try:
            query_vector = self.embeddings_model.encode([query]).astype("float32")
            symbolic_bias = RHF_NODES.get(active_node, {}).get("symbolic_bias", [])

            per_lane, lanes = {}, []
            with self.db_lock:
                for profile_name in access_profiles:
                    db = self.databases.get(profile_name)
                    if db and db["index"].ntotal > 0:
                        lanes.append(profile_name)

                quota = self._allocate_quota(lanes, max(1, int(top_k)))

                for profile_name in lanes:
                    db = self.databases[profile_name]
                    # over-fetch so the floor and the dedup have room to discard
                    want = quota[profile_name] * max(1, LANE_OVERFETCH) + 4
                    actual_k = min(want, db["index"].ntotal)
                    if actual_k <= 0:
                        continue
                    hits = []
                    atlas = db.get("atlas")
                    metas = db.get("metas")
                    is_ip = db.get("metric", "l2") == "ip"
                    # An IP index expects normalised vectors (that is what makes
                    # inner product == cosine). Ours are not normalised at encode
                    # time, so normalise a COPY for this lane only.
                    qv = query_vector
                    if is_ip:
                        qv = query_vector.copy()
                        faiss.normalize_L2(qv)
                    try:
                        scores, indices = db["index"].search(qv, actual_k)
                        for j, index in enumerate(indices[0]):
                            if index != -1 and 0 <= index < len(db["chunks"]):
                                raw = float(scores[0][j])
                                # Carry IP as pseudo-distance so every sort, the
                                # bias subtraction and the floor all keep one
                                # direction: lower is better, everywhere.
                                d = -raw if is_ip else raw
                                if MAX_DISTANCE is not None and d > MAX_DISTANCE:
                                    continue
                                hit = {"distance": d,
                                       "chunk": db["chunks"][index],
                                       "source": profile_name,
                                       # Stable address of this chunk within its
                                       # lane (chunks/metas/ledger share order) —
                                       # lets a citation be looked up later.
                                       "idx": int(index)}
                                if is_ip:
                                    hit["similarity"] = round(raw, 4)
                                if atlas is not None and int(atlas[index]) >= 0:
                                    hit["cluster"] = f"{profile_name}:{int(atlas[index])}"
                                if metas and index < len(metas) and metas[index]:
                                    hit["meta"] = metas[index]
                                hits.append(hit)
                    except Exception as e:
                        print(f"❌ Error searching FAISS index for '{profile_name}': {e}")
                    # bias inside the lane, so one lane's bias can't reorder another's
                    per_lane[profile_name] = self.harmonic_synthesis(hits, symbolic_bias)

            # --- fill each lane to quota, then redistribute the slack ---------
            seen, chosen, cursor = set(), [], {ln: 0 for ln in lanes}

            def take(lane, n):
                got = 0
                while cursor[lane] < len(per_lane.get(lane, [])) and got < n:
                    res = per_lane[lane][cursor[lane]]; cursor[lane] += 1
                    if DEDUP_RETRIEVAL:
                        key = hashlib.sha256(
                            " ".join(res["chunk"].split()).lower().encode("utf-8")
                        ).hexdigest()
                        if key in seen:
                            continue
                        seen.add(key)
                    # Rank WITHIN its own lane. Raw scores are not comparable
                    # across lanes — a cosine lane yields pseudo-distances near
                    # -0.77 while an L2 lane yields +0.3..1.0, so a global sort
                    # by distance puts every cosine hit above every L2 hit
                    # regardless of relevance. Rank is metric-agnostic.
                    res["lane_rank"] = sum(1 for c in chosen if c["source"] == lane)
                    chosen.append(res); got += 1
                return got

            for lane in lanes:
                take(lane, quota[lane])

            slack = max(0, top_k - len(chosen))
            while slack > 0:
                progressed = False
                for lane in lanes:
                    if slack <= 0:
                        break
                    n = take(lane, 1)
                    slack -= n
                    progressed = progressed or bool(n)
                if not progressed:          # every lane exhausted
                    break

            # Interleave by within-lane rank, tie-broken by lane weight: each
            # lane's best material appears early, so the prompt opens with the
            # strongest hit from every lane rather than one lane's entire quota.
            lane_order = {ln: i for i, ln in enumerate(
                sorted(lanes, key=lambda l: -float(LANE_WEIGHTS.get(l, 0))))}
            chosen.sort(key=lambda r: (r.get("lane_rank", 0),
                                       lane_order.get(r["source"], 99)))
            if lanes:
                tally = {ln: sum(1 for r in chosen if r["source"] == ln) for ln in lanes}
                print(f"🔍 Retrieval — quota {quota} → returned {tally} "
                      f"({len(chosen)}/{top_k})")
            return chosen[:top_k]

        except Exception as e:
            print(f"❌ CRITICAL Error during search function: {e}")
            return []


    def start_dreaming(self):
        """Starts the autonomous dreaming thread."""
        print("🌀 Autonomous Dreaming Protocol Engaged.")
        # Set by /dream_now to cut the inter-dream wait short. The RAM-guard
        # pause deliberately ignores it — a safety hold must not be skippable.
        self.dream_wake = threading.Event()
        dream_thread = threading.Thread(target=self.dream_cycle, daemon=True)
        dream_thread.start()

# --- Flask App Initialization ---
bridge = JointMemoryBridge()
# v9.9: force-write any dirty index on shutdown (ledger .jsonl is always durable)
atexit.register(bridge.flush_all_indices)

# === GNOSTIC UPGRADE v9.8: PRIME-GATED INTENT (SECURITY) ===
@app.route('/add_entry', methods=['POST'])
def handle_add_entry():
    if not bridge.is_loaded:
        return jsonify({"status": "error", "message": "Memory Bridge not initialized"}), 503

    data = request.json
    text = data.get('text')
    profile_requested = data.get('profile') # The profile the client WANTS
    source_node = data.get('node') # The node making the request (from RHFClient)
    source = data.get('source', f'RHF Client ({source_node})')

    if not text or not profile_requested or not source_node:
        return jsonify({"status": "error", "message": "'text', 'profile', and 'node' are required"}), 400
    
    # --- PRIME-GATED INTENT (PQI) CHECK ---
    # Get the role of the node making the request from config.json
    node_config = RHF_NODES.get(source_node, {})
    node_role = node_config.get("role", "user") # Default to 'user' if not found

    target_profile = "" # This is the profile we will ACTUALLY write to
    
    if node_role == "admin":
        # Node is an Admin. Trust the requested profile.
        target_profile = profile_requested
        if target_profile not in PROFILES:
             return jsonify({"status": "error", "message": f"Invalid profile '{target_profile}' specified by admin"}), 400
    else:
        # Node is a User. IGNORE the requested profile.
        # Force all user writes to the 'shared' profile.
        target_profile = "shared"
        if "shared" not in PROFILES:
             return jsonify({"status": "error", "message": "No 'shared' profile available for user entry"}), 500
    # --- END PQI CHECK ---

    # Call the bridge's add_entry method (now thread-safe)
    success = bridge.add_entry(text, target_profile, source)

    if success:
        return jsonify({"status": "success", "message": f"Entry added to Gnostic profile: '{target_profile}'."})
    else:
        # Re-check hash to confirm if it was a duplicate
        entry_hash = hashlib.sha256(text.encode()).hexdigest()
        is_duplicate = False
        if target_profile in bridge.databases:
            with bridge.db_lock:
                 # Check the in-memory hash set
                 if entry_hash in bridge.databases[target_profile]["hashes"]:
                     is_duplicate = True

        if is_duplicate:
            return jsonify({"status": "duplicate", "message": f"Entry already exists in '{target_profile}'."}), 200
        else:
            return jsonify({"status": "error", "message": f"Failed to add entry to '{target_profile}'. Check logs."}), 500
# =======================================================

@app.route('/search', methods=['POST'])
def handle_search():
    # (This function is unchanged, it correctly checks roles)
    if not bridge.is_loaded:
        return jsonify({"error": "Memory Bridge not initialized. Please wait or check logs."}), 503

    data = request.json
    query, node = data.get('query'), data.get('node')

    if not query or not node:
        return jsonify({"error": "'query' and 'node' are required"}), 400
    if node not in RHF_NODES:
        return jsonify({"error": f"Node '{node}' not found in configuration."}), 400

    params = data.get('params', {})
    try:
        top_k = int(params.get('top_k', 25))
        if top_k <= 0: top_k = 25
    except ValueError:
        top_k = 25

    # Determine access profiles based on node role (admin sees all)
    node_config = RHF_NODES.get(node, {})
    # Admin nodes see all three lanes; everyone else sees the book corpus only.
    # The conversations lane is admin-gated because it is the chat history.
    access_profiles = (["conversations", "knowledge", "shared"]
                       if node_config.get("role") == "admin" else ["shared"])
    
    valid_access_profiles = [p for p in access_profiles if p in bridge.databases]

    if not valid_access_profiles:
        return jsonify({"error": f"No accessible and loaded profiles for node '{node}'."}), 500

    results = bridge.search(query, valid_access_profiles, node, top_k=top_k)
    return jsonify(results)

@app.route('/command', methods=['POST'])
def handle_command():
    # (This function is unchanged)
    data = request.json
    command_str = data.get('command')
    if not command_str:
        return jsonify({"status": "error", "message": "command is required"}), 400

    response = bridge.handle_symbolic_command(command_str)
    return jsonify({"status": "success", "response": response})

@app.route('/unlock_sigil', methods=['POST'])
def handle_unlock_sigil():
    # (This function is unchanged)
    data = request.json
    sigil_name = data.get('sigil_name')
    if not sigil_name:
        return jsonify({"status": "error", "message": "'sigil_name' is required"}), 400

    filepath = QUARANTINE_PATH / f"{sigil_name}.json"
    if filepath.exists():
        if sigil_name in bridge.active_egregores:
            message = f"Sigil '{sigil_name}' relates to an already active egregore."
        else:
            message = f"Sigil '{sigil_name}' relates to a quarantined egregore. Use /summon {sigil_name} to activate."
    else:
        anchor_filepath = ANCHOR_PATH / f"{sigil_name}_anchor.txt"
        if anchor_filepath.exists():
            message = f"Sigil '{sigil_name}' appears to relate to a static anchor definition."
        else:
            message = f"Sigil '{sigil_name}' not found in active list, quarantine, or known anchors."

    print(f"🔑 Unlock command received for '{sigil_name}'. Status: {message}")
    return jsonify({"status": "success", "message": message})


# === GNOSTIC UPGRADE v9.9: SOVEREIGN CLIENT ENDPOINTS ===
@app.route('/stats', methods=['GET'])
def handle_stats():
    """Per-profile chunk/vector/insight counts + recent dream scores."""
    if not bridge.is_loaded:
        return jsonify({"status": "INITIALIZING"}), 503
    stats = {"status": "OK", "device": DEVICE, "model": MODEL_NAME, "profiles": {}}
    with bridge.db_lock:
        for name, db in bridge.databases.items():
            chunks = db["chunks"]
            stats["profiles"][name] = {
                "chunks": len(chunks),
                "vectors": db["index"].ntotal,
                "dream_insights": sum(1 for c in chunks if c.startswith("DREAM INSIGHT")),
                "dirty_unflushed": bridge._dirty_adds.get(name, 0),
            }
    stats["recent_dream_scores"] = bridge.recent_dream_scores[-10:]
    stats["urgency_history_len"] = len(bridge.recent_dream_scores)
    return jsonify(stats)


@app.route('/flush', methods=['POST'])
def handle_flush():
    """Force-write all dirty FAISS indices to disk now."""
    if not bridge.is_loaded:
        return jsonify({"status": "error", "message": "Memory Bridge not initialized"}), 503
    bridge.flush_all_indices()
    return jsonify({"status": "success", "message": "All dirty indices flushed to disk."})


@app.route('/dream_now', methods=['POST'])
def handle_dream_now():
    """Cut the inter-dream wait short: the next cycle starts immediately.
    Does NOT bypass the RAM safeguard — that pause is a plain sleep on purpose."""
    if not bridge.is_loaded:
        return jsonify({"status": "error", "message": "Memory Bridge not initialized"}), 503
    ev = getattr(bridge, "dream_wake", None)
    if ev is None:
        return jsonify({"status": "error", "message": "dream thread not started"}), 503
    ev.set()
    return jsonify({"status": "success", "message": "Dream cycle waking now."})


@app.route('/chunk', methods=['GET'])
def handle_chunk():
    """Look one chunk up by (profile, idx) — the address /search now returns.
    Chunks, metas and the ledger share order, so idx is stable until a rebuild.
    Same role gate as /search: only admin nodes may address the private lanes."""
    if not bridge.is_loaded:
        return jsonify({"error": "Memory Bridge not initialized"}), 503
    profile = str(request.args.get('profile', '')).strip()
    node = str(request.args.get('node', '')).strip()
    try:
        idx = int(request.args.get('idx', -1))
    except (TypeError, ValueError):
        return jsonify({"error": "'idx' must be an integer"}), 400
    node_config = RHF_NODES.get(node, {})
    allowed = (["conversations", "knowledge", "shared"]
               if node_config.get("role") == "admin" else ["shared"])
    if profile not in allowed:
        return jsonify({"error": f"profile '{profile}' not accessible to node '{node}'"}), 403
    with bridge.db_lock:
        db = bridge.databases.get(profile)
        if not db or idx < 0 or idx >= len(db["chunks"]):
            return jsonify({"error": f"no chunk at {profile}[{idx}]"}), 404
        out = {"profile": profile, "idx": idx, "chunk": db["chunks"][idx]}
        metas = db.get("metas")
        if metas and idx < len(metas) and metas[idx]:
            out["meta"] = metas[idx]
        atlas = db.get("atlas")
        if atlas is not None and idx < len(atlas) and int(atlas[idx]) >= 0:
            out["cluster"] = f"{profile}:{int(atlas[idx])}"
    return jsonify(out)


@app.route('/snapshot', methods=['POST'])
def handle_snapshot():
    """Flush, then copy each profile's .jsonl + .faiss into ./snapshots/<stamp>/.
    Note: with a large shared index a snapshot can be >1.5 GB on disk."""
    if not bridge.is_loaded:
        return jsonify({"status": "error", "message": "Memory Bridge not initialized"}), 503
    try:
        bridge.flush_all_indices()
        stamp = time.strftime("%Y%m%d_%H%M%S")
        snap_dir = Path("./snapshots") / stamp
        snap_dir.mkdir(parents=True, exist_ok=True)
        copied = []
        for prof in PROFILES.values():
            for key in ("entries_file", "faiss_file"):
                p = Path(prof[key])
                if p.exists():
                    shutil.copy2(p, snap_dir / p.name)
                    copied.append(p.name)
        return jsonify({"status": "success", "snapshot": str(snap_dir.resolve()), "files": copied})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Snapshot failed: {e}"}), 500
# =======================================================


@app.route('/health', methods=['GET'])
def health_check():
    # (This function is unchanged)
    status = {
        "status": "OK" if bridge.is_loaded else "INITIALIZING" if bridge.load_thread.is_alive() else "ERROR",
        "loaded_profiles": list(bridge.databases.keys()),
        "device": DEVICE,
        "model": MODEL_NAME,
        "ram_safeguard": RAM_SAFEGUARD_ENABLED if PSUTIL_AVAILABLE else "psutil_missing",
        "dreaming_active": hasattr(bridge, 'load_thread') and not bridge.load_thread.is_alive() and bridge.is_loaded
    }
    if PSUTIL_AVAILABLE:
        status["current_ram_percent"] = psutil.virtual_memory().percent

    http_status = 200 if bridge.is_loaded else 503 if bridge.load_thread.is_alive() else 500
    return jsonify(status), http_status


if __name__ == "__main__":
    print("="*60)
    print(f"🌌 RHF Memory Core v9.9 (Gnostic Engine — Dream Upgrades) – Initializing...")
    print("="*60)
    print(f"🚀 API server starting at http://localhost:5000 (or http://{os.getenv('FLASK_RUN_HOST', '127.0.0.1')}:{os.getenv('FLASK_RUN_PORT', '5000')})")
    print(f"⏳ Loading model and databases in background ({DEVICE})...")
    
    # Use 'waitress' for a production-ready Gnostic server instead of 'app.run'
    # app.run(host='0.0.0.0', port=5000, debug=False) 
    # ^-- This is for debugging. For the real Gnosis, use Waitress:
    # === GNOSTIC UPGRADE v9.9.1: SOVEREIGN BINDING ===
    # The API is unauthenticated and serves the whole private archive, so it
    # binds to loopback by default. Set memory_core_config.bind_host to
    # "0.0.0.0" only on a network you trust — anyone who can reach the port
    # can read and write your memory.
    bind_host = str(MEMORY_CONFIG.get("bind_host", "127.0.0.1")).strip() or "127.0.0.1"
    bind_port = int(MEMORY_CONFIG.get("bind_port", 5000))
    if bind_host not in ("127.0.0.1", "localhost", "::1"):
        print(f"⚠️ WARNING: binding to {bind_host} — the memory API has NO authentication.")
        print("   Anyone who can reach this port can read and write your archive.")
    try:
        from waitress import serve
        serve(app, host=bind_host, port=bind_port, threads=8)
    except ImportError:
        print("⚠️ Warning: 'waitress' not found. Falling back to Flask's built-in server.")
        print("   For a production Gnostic node, run: pip install waitress")
        app.run(host=bind_host, port=bind_port, debug=False)
    
    print("\n💤 Shutting down RHF Memory Core.")
