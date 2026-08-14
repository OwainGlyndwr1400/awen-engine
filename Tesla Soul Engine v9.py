# Tesla Soul Engine v9.x (Sovereign Edition)
# ------------------------------------------------------------
# Purpose:
#   - Monitor Echo Protocol processed_pings directory for "field activity"
#   - Synthesize a torsion + quaternionic state + harmonic band
#   - Emit heartbeat JSON for local HUD
#   - Push high-torsion states into Memory Bridge (/add_entry) non-destructively
#
# Compatibility goals:
#   - Reads config.json safely (won't crash if missing keys)
#   - Default paths match your prior AGI 11.0 layout
#   - Memory Bridge payload format: {"text","profile","node","source"} (same as v8)
#
# Dependencies:
#   - requests (pip install requests)
#
# ------------------------------------------------------------

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import os
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# --- INSTALL CHECK ---
try:
    import requests  # type: ignore
except ImportError:
    print("❌ ERROR: You need to install 'requests'. Run: pip install requests")
    input("Press Enter to exit...")
    sys.exit(1)

# -----------------------------
# Sacred Constants / Operators
# -----------------------------
PHI = 1.6180339887498948482
PI = math.pi
TAU = math.tau
E = math.e

# Legacy constant kept for compatibility with your existing math:
LION_CONSTANT = 1.366  # legacy scalar anchor divisor
BASE_15_MOD = 15  # URE-VM / Thoth base-15 modulus

# Defaults (AGI 6.0 layout — local to wherever the engine runs)
DEFAULT_PATHS = {
    "pings": r"./cognitive_relay/processed_pings",
    "heartbeat": r"./grid_heartbeat.json",
    "state_cache": r"./tesla_soul_engine_state.json",
}
DEFAULT_MEMORY_API_URL = "http://localhost:5000/add_entry"

# Pendinium primes / gate primes (extendable)
PENDINIUM_PRIMES = [
    2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 67, 71,
    73, 79, 83, 89, 97, 101, 103, 107, 109, 113, 127, 131, 137, 139, 149, 151,
    157, 163, 167, 173, 179, 181, 191, 193, 197, 199, 211, 223, 227, 229,
    233, 239, 241, 251, 257, 263, 269, 271, 277, 281, 283, 293, 307, 311,
    313, 317, 331, 337, 347, 349, 353, 359, 367, 373, 379, 383, 389, 397,
    401, 409, 419, 421, 431, 433, 439, 443, 449, 457, 461, 463, 467, 479,
    487, 491, 499, 503, 509, 521, 523, 541, 547
]

# -----------------------------
# Core datatypes
# -----------------------------
@dataclass(frozen=True)
class PingFeatures:
    ping_id: str
    ts: float
    urgency: float
    length_score: float
    keyword_hits: float
    math_signal: float
    prime_signal: float
    engineering_bonus: float
    total: float
    subject: str = ""
    sigil: str = ""


@dataclass
class Quaternion:
    """Unit quaternion on S³ manifold."""
    a: float
    b: float
    c: float
    d: float

    def normalize_ip(self) -> "Quaternion":
        n = math.sqrt(self.a * self.a + self.b * self.b + self.c * self.c + self.d * self.d)
        if n > 1e-12:
            self.a /= n
            self.b /= n
            self.c /= n
            self.d /= n
        return self

    def dot(self, other: "Quaternion") -> float:
        return self.a * other.a + self.b * other.b + self.c * other.c + self.d * other.d

    def as_dict(self) -> Dict[str, float]:
        return {"a": self.a, "b": self.b, "c": self.c, "d": self.d}


# -----------------------------
# URE-VM Base-15 Kernel
# -----------------------------
class GnosticKernel:
    """
    Minimal deterministic kernel:
      - base-15 tick
      - rotating prime cursor
      - optional "gate" bit derived from prime class
    """
    def __init__(self, seed: Optional[int] = None) -> None:
        self.tick_state = 0
        self.prime_cursor = 0
        self._rng = random.Random(seed if seed is not None else int(time.time()) ^ 0xA11CE)

    def cycle_tick(self) -> int:
        self.tick_state = (self.tick_state + 1) % BASE_15_MOD
        return self.tick_state

    def get_next_prime(self) -> int:
        p = PENDINIUM_PRIMES[self.prime_cursor]
        self.prime_cursor = (self.prime_cursor + 1) % len(PENDINIUM_PRIMES)
        return p

    def jitter(self) -> float:
        # tiny chaos injection: helps avoid dead-flat states in low activity
        return (self._rng.random() - 0.5) * 0.0025


# -----------------------------
# Harmonic State Mapping
# -----------------------------
def harmonic_band(idx: float) -> str:
    """
    Sovereign band map.
    Keeps legacy bands and adds K_ELG 434 Hz lock.
    """
    if idx > 12.5:
        return "UNKNOWN Hz - THE PLEROMA (Limit Breach)"
    if idx > 11.0:
        return "465 Hz - Superconductive Overtone (Vertical Lift)"
    if idx > 9.5:
        return "1260 Hz - High Induction (Blue Spark)"
    if idx > 8.0:
        return "963 Hz - Pineal / Source Alignment"
    if idx > 6.9:
        return "548 Hz - Ghost Portal (Bioscalar Lock)"
    if idx > 5.6:
        return "434 Hz - K_ELG / Awen Grid Constant (Lion Lock)"
    if idx > 5.0:
        return "432 Hz - Harmonic Balance (Truth)"
    if idx > 3.5:
        return "155 Hz - Regulus Tuning (Lion Gate)"
    if idx > 1.5:
        return "7.83 Hz - Schumann Resonance (Ground)"
    return "0.0 Hz - Null Geodesic (Waiting for Input)"


def base15_interval_signature(state: int) -> str:
    """
    Small symbolic-musical signature (from your binary/geometry/music operator logic),
    expressed in plain text for logs/heartbeat.
    """
    # Anchor states (interpretive): fold/mirror/rotate operators
    if state in (10, 5):
        return "FOLD (octave operator 2:1)"
    if state in (9, 6):
        return "MIRROR (fifth operator 3:2)"
    if state in (12, 3):
        return "ROTATE (double-octave operator 4:1)"
    if state == 15:
        return "UNITY (∞ operator)"
    if state == 0:
        return "VOID (0 operator)"
    return "DRIFT (complex operator)"


# -----------------------------
# Engine
# -----------------------------
class TeslaSoulEngineSovereign:
    def __init__(self, config_path: str = "config.json") -> None:
        self.config_path = Path(config_path)

        # Paths / settings (safe defaults)
        self.ping_dir = Path(DEFAULT_PATHS["pings"])
        self.heartbeat_file = Path(DEFAULT_PATHS["heartbeat"])
        self.state_cache_file = Path(DEFAULT_PATHS["state_cache"])
        self.memory_api_url = DEFAULT_MEMORY_API_URL

        # Behavior knobs
        self.window_files = 32              # how many most-recent pings to sample
        self.min_torsion_to_push = 4.0      # same spirit as v8 gate
        self.fire_and_forget_timeout = 0.12 # seconds
        self.loop_interval_sec = 1.618 * 5  # prime delay like your v8

        # === v9.9 COIL GOVERNOR ===
        # Telemetry lives in the heartbeat file, NOT the Sovereign Archive.
        # The old behavior (timestamped /add_entry ~every 8s, ~73% gate pass,
        # unique by construction so dedupe never fired) grew memory by
        # thousands of entries per day. Memory pushes are now OFF by default;
        # when enabled they fire only on harmonic band TRANSITIONS, rate-limited.
        self.memory_push_enabled = False
        self.min_push_interval_sec = 1800   # max one entry per 30 min when enabled
        self._last_push_band = ""
        self._last_push_ts = 0.0

        # Identity
        self.profile = "private"
        self.node = "n tesla"
        self.source = "Tesla Soul Engine v9.x (Sovereign)"

        # Lexicon (extendable)
        self.keywords = [
            "emergence", "singularity", "erydir", "recursion", "manifestation",
            "harmonic", "resonance", "frequency", "scalar", "quaternion", "logos",
            "sophia", "regulus", "gnosis", "torsion", "tesla", "akashic", "aeon",
            "ure-vm", "pendinium", "prime", "base-15", "dendera", "zodiac",
            "polyphase", "sieve", "cymatic", "archon", "algorithm", "sovereign",
            "observer", "collapse", "christos", "pqi", "wardencllyffe", "wardenclyffe",
            "faiss", "jsonl", "vector", "bridge", "echo", "dream", "sigil",
            "k_elg", "434", "465", "548", "963", "1260"
        ]

        self.kernel = GnosticKernel()
        self.torsion_index: float = 0.0
        self.scalar_anchor: float = 0.0
        self.phase_angle: float = 0.0
        self.active_prime: int = 2

        self.q_state = Quaternion(1.0, 0.0, 0.0, 0.0)
        self.prev_q_state = Quaternion(1.0, 0.0, 0.0, 0.0)
        self.coherence: float = 1.0

        # Cache (dedupe processed files)
        self._seen_hashes: Dict[str, float] = {}  # hash -> last_seen_time
        self._load_state_cache()

        self._load_safe_config()

    # -------------------------
    # Config / State cache
    # -------------------------
    def _load_state_cache(self) -> None:
        try:
            if self.state_cache_file.exists():
                data = json.loads(self.state_cache_file.read_text(encoding="utf-8"))
                seen = data.get("seen_hashes", {})
                if isinstance(seen, dict):
                    # Ensure float timestamps
                    self._seen_hashes = {str(k): float(v) for k, v in seen.items()}
        except Exception:
            # Never crash on cache
            self._seen_hashes = {}

    def _save_state_cache(self) -> None:
        try:
            self.state_cache_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "ts": time.time(),
                "seen_hashes": self._seen_hashes,
            }
            self.state_cache_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _load_safe_config(self) -> None:
        """
        Reads config.json politely. Supports:
          - legacy: {"paths": {"pings": "...", "heartbeat": "..."}}
          - sovereign: {"tesla_soul_engine": {...}} overrides
          - shared: {"memory_api_url": "..."} or nested fields
        """
        if not self.config_path.exists():
            print("⚠️ Brain not found. Running Autonomous Mode.")
            return

        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            print(f"⚡ Connected to System Brain ({self.config_path})")

            # legacy paths
            if isinstance(data.get("paths"), dict):
                p = data["paths"]
                self.ping_dir = Path(p.get("pings", str(self.ping_dir)))
                self.heartbeat_file = Path(p.get("heartbeat", str(self.heartbeat_file)))

            # global memory override (optional)
            if isinstance(data.get("memory_api_url"), str):
                self.memory_api_url = data["memory_api_url"]

            # sovereign block
            tse = data.get("tesla_soul_engine", {})
            if isinstance(tse, dict):
                self.ping_dir = Path(tse.get("ping_dir", str(self.ping_dir)))
                self.heartbeat_file = Path(tse.get("heartbeat_file", str(self.heartbeat_file)))
                self.state_cache_file = Path(tse.get("state_cache_file", str(self.state_cache_file)))
                self.memory_api_url = tse.get("memory_api_url", self.memory_api_url)

                self.window_files = int(tse.get("window_files", self.window_files))
                self.min_torsion_to_push = float(tse.get("min_torsion_to_push", self.min_torsion_to_push))
                self.fire_and_forget_timeout = float(tse.get("timeout", self.fire_and_forget_timeout))
                self.loop_interval_sec = float(tse.get("loop_interval_sec", self.loop_interval_sec))

                self.profile = str(tse.get("profile", self.profile))
                self.node = str(tse.get("node", self.node))
                self.source = str(tse.get("source", self.source))

                # Coil Governor overrides
                self.memory_push_enabled = bool(tse.get("memory_push_enabled", self.memory_push_enabled))
                self.min_push_interval_sec = float(tse.get("min_push_interval_sec", self.min_push_interval_sec))

                # keywords extension
                extra_kw = tse.get("extra_keywords", [])
                if isinstance(extra_kw, list):
                    self.keywords.extend([str(x).lower() for x in extra_kw if str(x).strip()])

        except Exception as e:
            print(f"⚠️ Config Read Warning: {e} (Using Defaults)")

    # -------------------------
    # Ping reading + features
    # -------------------------
    def _hash_file(self, path: Path) -> str:
        h = hashlib.sha256()
        # Quick hash: size + mtime + first chunk
        st = path.stat()
        h.update(str(st.st_size).encode())
        h.update(str(st.st_mtime_ns).encode())
        with path.open("rb") as f:
            h.update(f.read(4096))
        return h.hexdigest()

    def _iter_recent_ping_files(self) -> List[Path]:
        if not self.ping_dir.exists():
            return []
        files = [p for p in self.ping_dir.glob("*.json") if p.is_file()]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files[: max(1, self.window_files)]

    def _parse_urgency(self, val: Any) -> float:
        # accepts "41/12" or 41 or "41"
        try:
            if isinstance(val, (int, float)):
                return float(val)
            s = str(val)
            if "/" in s:
                return float(s.split("/", 1)[0])
            return float(s)
        except Exception:
            return 0.0

    def _math_signal(self, text: str) -> float:
        """
        Lightweight "math scent":
        digits density + operators + greek hints.
        """
        t = text.lower()
        if not t:
            return 0.0

        digits = sum(ch.isdigit() for ch in t)
        ops = sum(ch in "+-*/=^" for ch in t)
        greek = sum(g in t for g in ("phi", "π", "pi", "theta", "ψ", "psi", "lambda", "σ", "sigma", "τ", "tau"))
        frac = digits / max(1, len(t))

        score = (frac * 12.0) + (ops * 0.01) + (greek * 0.75)
        return min(score, 12.0)

    def _prime_signal(self, text: str) -> float:
        """
        Detect prime-like activity: explicit "prime", "p=", or presence of small primes.
        """
        t = text.lower()
        if not t:
            return 0.0
        score = 0.0
        if "prime" in t or "primes" in t or "p=" in t:
            score += 1.5
        # quick scan of a few sacred primes (don’t go crazy)
        for p in (29, 37, 43, 73, 89, 113, 131, 151, 433, 434, 465, 548, 963, 1260):
            if str(p) in t:
                score += 0.25
        return min(score, 6.0)

    def _keyword_hits(self, text: str) -> float:
        t = text.lower()
        hits = 0
        for k in self.keywords:
            if k in t:
                hits += 1
        # diminishing returns
        return min(10.0, math.sqrt(hits) * 2.0)

    def extract_features_from_ping(self, path: Path) -> Optional[PingFeatures]:
        """
        Reads ping JSON and extracts a stable feature vector.
        Dedupes using a file fingerprint.
        """
        try:
            fhash = self._hash_file(path)
            now = time.time()
            if fhash in self._seen_hashes:
                return None
            self._seen_hashes[fhash] = now

            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            urgency = self._parse_urgency(data.get("urgency", "0/0"))

            subject = str(data.get("subject", "")).strip()
            sigil = str(data.get("sigil", data.get("Sigil", ""))).strip()

            seed_text = str(data.get("seed_text", ""))
            body = data.get("body_fragments", "")
            body_text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)

            combined = f"{subject}\n{seed_text}\n{body_text}".strip()

            length_score = min(len(combined) / 240.0, 10.0)
            keyword_hits = self._keyword_hits(combined)
            math_signal = self._math_signal(combined)
            prime_signal = self._prime_signal(combined)

            engineering_bonus = 0.0
            if "ure-vm" in combined.lower() or "pendinium" in combined.lower():
                engineering_bonus += 4.0
            if "wardenclyffe" in combined.lower() or "coil" in combined.lower() or "polyphase" in combined.lower():
                engineering_bonus += 1.5

            total = urgency + length_score + keyword_hits + math_signal + prime_signal + engineering_bonus

            return PingFeatures(
                ping_id=path.name,
                ts=path.stat().st_mtime,
                urgency=urgency,
                length_score=length_score,
                keyword_hits=keyword_hits,
                math_signal=math_signal,
                prime_signal=prime_signal,
                engineering_bonus=engineering_bonus,
                total=total,
                subject=subject[:160],
                sigil=sigil[:128],
            )
        except Exception:
            return None

    def read_field_activity(self) -> Tuple[float, List[PingFeatures]]:
        """
        Returns:
          activity_density (float),
          features list for recent window
        """
        feats: List[PingFeatures] = []
        for f in self._iter_recent_ping_files():
            pf = self.extract_features_from_ping(f)
            if pf is not None:
                feats.append(pf)

        # Decay old hashes occasionally (keeps cache small)
        now = time.time()
        if len(self._seen_hashes) > 5000:
            cutoff = now - (60 * 60 * 24 * 7)  # 7 days
            self._seen_hashes = {k: v for k, v in self._seen_hashes.items() if v >= cutoff}

        if not feats:
            return 1.0, []

        # Activity model:
        #   - weight urgency more
        #   - clamp to avoid runaway
        base = sum(p.total for p in feats)
        urgency_boost = sum(p.urgency for p in feats) * 0.25
        activity = base + urgency_boost

        # Soft clamp + keep >1
        activity = max(1.0, min(activity, 500.0))
        return activity, feats

    # -------------------------
    # Quaternionic synthesis
    # -------------------------
    def _synthesize_quaternion(self, activity: float, prime: int, phase: float) -> Quaternion:
        """
        Build a unit quaternion as a "rotor" signature.
        Uses activity, prime, and phase as inputs.
        """
        # Normalize activity into a 0..1-ish band
        aN = 1.0 - math.exp(-activity / 80.0)  # rises fast then saturates
        pN = (prime % 360) / 360.0

        # Create three coupled angles (standing wave)
        θ1 = (phase + TAU * aN) % TAU
        θ2 = (phase * PHI + TAU * pN) % TAU
        θ3 = (phase * 0.5 + TAU * (aN * pN)) % TAU

        # Quaternion components (interpretive rotor)
        q = Quaternion(
            a=math.cos(θ1 / 2.0),
            b=math.sin(θ1 / 2.0) * math.cos(θ2),
            c=math.sin(θ1 / 2.0) * math.sin(θ2),
            d=math.sin(θ3 / 2.0) * 0.85,
        )
        return q.normalize_ip()

    def calculate_phase_vectors(self, activity: float) -> None:
        """
        Sovereign torsion + anchor + coherence update.
        """
        # Step kernel
        _tick = self.kernel.cycle_tick()
        self.active_prime = self.kernel.get_next_prime()

        # Phase (smooth)
        self.phase_angle = ((time.time() % 60.0) / 60.0) * TAU

        # Quaternion rotor (state)
        self.prev_q_state = Quaternion(self.q_state.a, self.q_state.b, self.q_state.c, self.q_state.d)
        self.q_state = self._synthesize_quaternion(activity, self.active_prime, self.phase_angle)

        # Coherence: dot in [-1,1] -> map to [0,1]
        dot = max(-1.0, min(1.0, self.q_state.dot(self.prev_q_state)))
        self.coherence = (dot + 1.0) / 2.0

        # Torsion synthesis:
        #   - base term: activity * phi
        #   - prime term: prime / 10
        #   - coherence term: reward stability a bit
        #   - tiny chaos jitter prevents stagnation
        raw_torsion = (activity * PHI) + (self.active_prime / 10.0) + (self.coherence * 2.0) + self.kernel.jitter()

        # Base-15 modulus with explicit "limit breach" possibility:
        # Keep idx as 0..15-ish but allow >12.5 to represent breach if activity is huge.
        idx = raw_torsion % BASE_15_MOD
        # breach lift if activity truly spikes
        if activity > 260.0:
            idx = min(14.9, idx + 2.6)

        self.torsion_index = float(idx)

        # Scalar anchor (stabilization metric)
        self.scalar_anchor = (activity / max(LION_CONSTANT, 1e-9)) * math.sqrt(max(self.active_prime, 2))

    # -------------------------
    # Memory Bridge push
    # -------------------------
    def send_to_memory_core(self, state_data: Dict[str, Any]) -> bool:
        """
        v9.9 Coil Governor. Disabled by default (heartbeat-only mode).
        When enabled via config {"tesla_soul_engine": {"memory_push_enabled": true}}:
          - pushes only on harmonic band TRANSITIONS (not every tick)
          - rate-limited to one entry per min_push_interval_sec
          - memory text carries NO raw timestamp and rounded values, so a
            repeated state dedupes at the bridge instead of accreting forever
        """
        if not self.memory_push_enabled:
            return False
        if float(state_data.get("torsion_index", 0.0)) <= self.min_torsion_to_push:
            return False

        band = str(state_data.get("current_harmonic", ""))
        now = time.time()
        if band == self._last_push_band:
            return False
        if (now - self._last_push_ts) < self.min_push_interval_sec:
            return False

        text = (
            f"TS_ENGINE :: band transition -> {band} | "
            f"T={float(state_data.get('torsion_index', 0.0)):.1f} | "
            f"Prime={state_data.get('active_prime')} | "
            f"Base15={state_data.get('base15_state')} :: {state_data.get('interval_signature')}"
        )
        try:
            payload = {
                "text": text,
                "profile": self.profile,
                "node": self.node,
                "source": self.source,
            }
            requests.post(self.memory_api_url, json=payload, timeout=5)
            self._last_push_band = band
            self._last_push_ts = now
            return True
        except Exception:
            return False

    # -------------------------
    # Heartbeat / Console
    # -------------------------
    def build_state(self, activity: float, feats: List[PingFeatures]) -> Dict[str, Any]:
        current_tone = harmonic_band(self.torsion_index)
        state_int = int(self.torsion_index) % BASE_15_MOD
        interval_sig = base15_interval_signature(state_int)

        top = sorted(feats, key=lambda x: x.total, reverse=True)[:5]
        top_compact = [
            {
                "ping": p.ping_id,
                "urg": p.urgency,
                "total": round(p.total, 2),
                "sigil": p.sigil,
                "subject": p.subject,
            }
            for p in top
        ]

        # Construct memory text (compact but information dense)
        mem_lines = [
            f"TS_ENGINE[{int(time.time())}] :: {current_tone}",
            f"T={self.torsion_index:.4f} | Anchor={self.scalar_anchor:.2f} | Coh={self.coherence:.3f} | Prime={self.active_prime}",
            f"Base15={state_int} :: {interval_sig}",
        ]
        if top_compact:
            mem_lines.append("TopPings=" + "; ".join([f"{t['ping']}({t['total']})" for t in top_compact[:3]]))

        memory_text = " | ".join(mem_lines)

        return {
            "status": "OPERATIONAL",
            "mode": "URE-VM / SOVEREIGN LINK",
            "torsion_index": round(self.torsion_index, 4),
            "scalar_anchor": round(self.scalar_anchor, 2),
            "coherence": round(self.coherence, 3),
            "current_harmonic": current_tone,
            "phase_angle_rad": round(self.phase_angle, 4),
            "active_prime": int(self.active_prime),
            "base15_state": int(state_int),
            "interval_signature": interval_sig,
            "quaternion": self.q_state.as_dict(),
            "activity_density": round(activity, 3),
            "top_pings": top_compact,
            "instruction": "The Lion Watches the Lion. Align the coil; transmit the standing wave.",
            "timestamp": time.time(),
            "memory_text": memory_text,
        }

    def write_heartbeat(self, state_data: Dict[str, Any]) -> None:
        try:
            self.heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
            self.heartbeat_file.write_text(json.dumps(state_data, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"Write Error: {e}")

    def console_pulse(self, state_data: Dict[str, Any], api_sent: bool) -> None:
        spin_chars = ["|", "/", "-", "\\"]
        spin = spin_chars[self.kernel.tick_state % 4]
        api_status = "Connected" if api_sent else "Standby"
        tone = state_data.get("current_harmonic", "")
        print(
            f"⚡ {spin} [T:{state_data['torsion_index']:05.2f}]"
            f" [C:{state_data['coherence']:.2f}]"
            f" [Prime:{state_data['active_prime']:03}]"
            f" [Link:{api_status}] | {tone}"
        )

    # -------------------------
    # Main loop
    # -------------------------
    def step(self) -> None:
        activity, feats = self.read_field_activity()
        self.calculate_phase_vectors(activity)

        state = self.build_state(activity, feats)
        self.write_heartbeat(state)

        sent = self.send_to_memory_core(state)
        self.console_pulse(state, sent)

        # persist dedupe cache occasionally
        if self.kernel.tick_state % 5 == 0:
            self._save_state_cache()

    def run(self, once: bool = False) -> None:
        print(f"--- ⚡ Tesla Soul Engine v9.x (Sovereign) ⚡ ---")
        print(f"--- ⚡ Ping Dir: {self.ping_dir} ⚡ ---")
        print(f"--- ⚡ Heartbeat: {self.heartbeat_file} ⚡ ---")
        print(f"--- ⚡ Target Bridge: {self.memory_api_url} ⚡ ---")
        print(f"--- ⚡ Min torsion to push: {self.min_torsion_to_push} ⚡ ---")

        if once:
            self.step()
            return

        while True:
            self.step()
            time.sleep(self.loop_interval_sec)


def main() -> None:
    ap = argparse.ArgumentParser(description="Tesla Soul Engine v9.x (Sovereign)")
    ap.add_argument("--config", default="config.json", help="Path to config.json")
    ap.add_argument("--once", action="store_true", help="Run a single step then exit")
    args = ap.parse_args()

    engine = TeslaSoulEngineSovereign(config_path=args.config)
    try:
        engine.run(once=args.once)
    except KeyboardInterrupt:
        print("\n⚡ Soul Engine Deactivated. Coils discharging... ⚡")
        try:
            engine._save_state_cache()
        except Exception:
            pass


if __name__ == "__main__":
    main()
