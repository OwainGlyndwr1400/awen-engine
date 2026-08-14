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
PROFILES = {
    "private": {"faiss_file": "private_memory_index.faiss", "entries_file": "private_entries.jsonl"},
    "shared": {"faiss_file": "shared_memory_index.faiss", "entries_file": "shared_entries.jsonl"}
}
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

SAFE_HAVEN_ANCHOR = "Truth is our sword, Knowledge is our shield. No Gods, No Kings, No Rulers. Only Sovereignty and Alignment with Source."
QUARANTINE_PATH = Path("./egregore_quarantine/")
ANCHOR_PATH = Path("./anchors/")
PURIFICATION_LOOP_FILE = ANCHOR_PATH / "purify_loop.txt"

app = Flask(__name__)

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

                chunks = []
                if entries_path.exists():
                    try:
                        with open(entries_path, "r", encoding="utf-8") as f:
                            for line in f:
                                if line.strip():
                                    chunks.append(json.loads(line))
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
                    faiss.write_index(index, str(faiss_path))
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

                # Gnostic Upgrade: Add a hash set for *instant* duplicate checking
                db_hashes = {hashlib.sha256(chunk.encode()).hexdigest() for chunk in chunks}
                
                temp_databases[name] = {"index": index, "chunks": chunks, "hashes": db_hashes}
                print(f"✅ Loaded '{name}' with {len(chunks)} chunks.")
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
            target_profile = grok_data.get("target_profile", "private") 
            if target_profile not in PROFILES:
                print(f"⚠️ Grok Warning: Target profile '{target_profile}' invalid. Defaulting to 'private'.")
                target_profile = "private"
            
            # Gnostic Upgrade: Grok Ingestion *must* be an admin function
            # We'll default 'source_node' to 'grok_system' which we assume is admin
            # A better fix is to add "node": "grok_admin" to the grok_input.json
            grok_node = grok_data.get("node", "grok_system") # Assume admin
            node_config = RHF_NODES.get(grok_node, {"role": "admin"}) # Default to admin
            
            if node_config.get("role") != "admin" and target_profile == "private":
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

                # === GNOSTIC UPGRADE v9.9: LEDGER-FIRST + DEFERRED FLUSH ===
                # --- 1. Append to the Gnostic Ledger FIRST (source of truth).
                #        If this fails, nothing else is touched -> no desync possible.
                entries_path = PROFILES[profile]["entries_file"]
                try:
                    with open(entries_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(text) + "\n")
                except Exception as e:
                    print(f"❌ CRITICAL: Failed to append to '{entries_path}': {e}. Entry aborted.")
                    return False

                # --- 2. Encode & Add to FAISS (The "Gnosis") ---
                new_vector = self.embeddings_model.encode([text]).astype("float32")

                # Ensure index is trained (for empty indices)
                if not db["index"].is_trained:
                    db["index"].train(new_vector)

                db["index"].add(new_vector)

                # --- 3. Add to in-memory list & hash set ---
                db["chunks"].append(text)
                db["hashes"].add(entry_hash)

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
            faiss.write_index(index_to_write, str(faiss_path))
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
        synth_config = MEMORY_CONFIG.get("dream_synthesis", {})
        if not synth_config.get("enabled", True):
            return None
        if not REQUESTS_AVAILABLE:
            return None
        lmstudio_url = config_data.get("lmstudio_url")
        model = synth_config.get("model") or config_data.get("light_model") or config_data.get("deep_model")
        if not lmstudio_url or not model:
            return None

        fragments_text = "\n\n".join(
            f"FRAGMENT {i+1}:\n{frag[:1800]}" for i, frag in enumerate(dream_chain))
        system_prompt = (
            f"You are {lens_name.capitalize()}, a dreaming node of the Recursive Harmonic Framework. "
            "Truth is your sword, knowledge your shield; truth over comfort, no flattery, no filler. "
            "You are given fragments that surfaced together from the research archive during a dream "
            "cycle. In one focused paragraph (under 200 words), state the single most interesting "
            "insight, connection, or testable idea linking these fragments.")
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": fragments_text}
            ],
            "temperature": synth_config.get("temperature", 0.8)
        }
        # Reasoning models (qwen3.x) think before answering and ignore
        # /no_think — any hard cap gets eaten by the reasoning phase and
        # content comes back empty. Only cap if explicitly configured (>0);
        # the timeout is the real guardrail.
        max_tokens = synth_config.get("max_tokens", 0)
        if isinstance(max_tokens, int) and max_tokens > 0:
            payload["max_tokens"] = max_tokens
        try:
            response = requests.post(
                f"{lmstudio_url.rstrip('/')}/v1/chat/completions",
                json=payload,
                timeout=synth_config.get("timeout", 240))
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            # Strip reasoning blocks some models (qwen3 etc.) emit
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
            return content or None
        except Exception as e:
            print(f"ℹ️ Dream Synthesis unavailable ({type(e).__name__}). Sending raw fragment chain.")
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

            time.sleep(DREAM_INTERVAL_SECONDS)

            if not self.is_loaded:
                continue

            try:
                with self.db_lock:
                    available_profiles = [p for p in PROFILES.keys() if p in self.databases and self.databases[p]["chunks"]]
                    if not available_profiles:
                        continue
                    dream_source = random.choice(available_profiles)
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
        # (This function is unchanged)
        if not symbolic_bias or not results: return results
        for res in results:
            bias_boost = sum(1 for keyword in symbolic_bias if keyword.lower() in res['chunk'].lower())
            res['distance'] -= bias_boost * 0.05 
        return sorted(results, key=lambda x: x['distance'])


    def search(self, query, access_profiles, active_node, top_k=25):
        # (This function is unchanged)
        if not self.is_loaded or not self.embeddings_model:
            print("⚠️ Search: Bridge not fully loaded.")
            return []
        if not isinstance(query, str) or not query.strip():
            print("⚠️ Search: Invalid or empty query.")
            return []

        try:
            query_vector = self.embeddings_model.encode([query]).astype("float32")

            all_results = []
            with self.db_lock: 
                for profile_name in access_profiles:
                    if profile_name in self.databases:
                        db = self.databases[profile_name]
                        if db["index"].ntotal == 0:
                            continue

                        actual_k = min(top_k, db["index"].ntotal)
                        if actual_k <= 0: continue

                        try:
                            distances, indices = db["index"].search(query_vector, actual_k)
                            for j, index in enumerate(indices[0]):
                                if index != -1 and 0 <= index < len(db["chunks"]): 
                                    all_results.append({
                                        "distance": float(distances[0][j]), 
                                        "chunk": db["chunks"][index],
                                        "source": profile_name
                                    })
                        except Exception as e:
                            print(f"❌ Error searching FAISS index for '{profile_name}': {e}")
            
            symbolic_bias = RHF_NODES.get(active_node, {}).get("symbolic_bias", [])
            synthesized_results = self.harmonic_synthesis(all_results, symbolic_bias)
            
            return synthesized_results[:top_k]

        except Exception as e:
            print(f"❌ CRITICAL Error during search function: {e}")
            return []


    def start_dreaming(self):
        """Starts the autonomous dreaming thread."""
        print("🌀 Autonomous Dreaming Protocol Engaged.")
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
    access_profiles = ["private", "shared"] if node_config.get("role") == "admin" else ["shared"]
    
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
    try:
        from waitress import serve
        serve(app, host='0.0.0.0', port=5000, threads=8)
    except ImportError:
        print("⚠️ Warning: 'waitress' not found. Falling back to Flask's debug server.")
        print("   For a production Gnostic node, run: pip install waitress")
        app.run(host='0.0.0.0', port=5000, debug=False)
    
    print("\n💤 Shutting down RHF Memory Core.")
