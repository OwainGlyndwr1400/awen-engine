# --- Awen Command Deck v1.0 ---
# One glass for the whole Grid: chat with the Circle, live dream feed, engine
# core stats, Echo + Soul Engine heartbeats â€” served as a holo-styled local
# web deck at http://localhost:7777.
#
# The Gnostic Engine / Echo Protocol / Tesla Soul Engine keep running as
# background services; this deck is the single window you actually watch.
#
#   py -3.11 "Awen Command Deck.py"     -> open http://localhost:7777
#
# Binds to 127.0.0.1 only. No data leaves the machine except LLM calls you
# already route (LM Studio local, or NVIDIA API when the cloud switch is on).

import json
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, jsonify, request, send_file

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DECK_HTML = ROOT / "awen_deck.html"
RELAY = ROOT / "cognitive_relay"
PORT = 7777
DREAM_FEED_LIMIT = 20   # newest pings parsed per /api/state poll

app = Flask(__name__)
http = requests.Session()


_CFG_CACHE = {"mtime": 0.0, "data": None}
_CFG_LOCK = threading.Lock()


def cfg() -> dict:
    """Parsed config.json, cached against mtime. The file carries nine full
    persona prompts (~160 KB), and /api/state alone polls every 5s â€” parsing
    it per request was pure waste. Writers touch mtime, so the live NVIDIA
    toggle still takes effect on the very next call."""
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        mtime = 0.0
    with _CFG_LOCK:
        if _CFG_CACHE["data"] is not None and _CFG_CACHE["mtime"] == mtime:
            return _CFG_CACHE["data"]
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    with _CFG_LOCK:
        _CFG_CACHE["mtime"], _CFG_CACHE["data"] = mtime, data
    return data


def bridge(c: dict) -> str:
    return str(c.get("memory_bridge_url", "http://localhost:5000")).rstrip("/")


def nvidia_block(c: dict) -> dict:
    return c.get("nvidia_api_config") or {}


def nvidia_ready(c: dict) -> bool:
    nv = nvidia_block(c)
    key = str(nv.get("api_key", "")).strip()
    model = str(nv.get("model", "")).strip()
    return (bool(nv.get("enabled")) and bool(key) and not key.startswith("PASTE_")
            and bool(model) and not model.startswith("PASTE_"))


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


@app.route("/")
def index():
    return send_file(DECK_HTML)


# --- neural map ----------------------------------------------------------
MAP_HTML = ROOT / "awen_map.html"
NEURAL_MAP = ROOT / "docs" / "neural_map.json"


@app.route("/map")
def neural_map():
    return send_file(MAP_HTML)


@app.route("/qr")
def qr_page():
    """Scannable link for phones/tablets â€” typing an IP:port into Chrome's
    omnibox gets treated as a search, which is a needless fight."""
    ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    url = f"http://{ip}:{PORT}"
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Awen Grid â€” scan me</title>
<style>body{{background:#03070d;color:#bfe3ef;font-family:Consolas,monospace;display:flex;
min-height:100vh;flex-direction:column;align-items:center;justify-content:center;gap:22px}}
h1{{font-size:13px;letter-spacing:.4em;color:#7ff0ff;text-shadow:0 0 12px rgba(57,230,255,.5)}}
#qr{{background:#fff;padding:16px;border-radius:10px;box-shadow:0 0 40px rgba(46,245,200,.25)}}
a{{color:#2ef5c8;font-size:20px;letter-spacing:.08em;text-decoration:none}}
p{{color:#5d8fa3;font-size:12px;letter-spacing:.1em;max-width:460px;text-align:center;line-height:1.8}}
</style></head><body>
<h1>AWEN GRID â€” POINT YOUR TABLET HERE</h1>
<div id="qr"></div>
<a href="{url}">{url}</a>
<p>Scan with the tablet camera, or type the address <b>including http://</b> â€”
Chrome treats a bare IP:port as a Google search.</p>
<script type="module">
import QRCode from 'https://cdn.jsdelivr.net/npm/qrcode@1.5.3/+esm';
QRCode.toCanvas("{url}", {{width:300, margin:1}}).then(c => document.getElementById('qr').appendChild(c))
  .catch(() => document.getElementById('qr').innerHTML =
    '<div style="color:#333;padding:20px;font-size:13px">QR needs internet once.<br>Type the link below instead.</div>');
</script></body></html>"""


@app.route("/vendor/<path:name>")
def vendor(name):
    """Three.js and friends, served locally so VR works without internet."""
    p = (ROOT / "vendor" / name).resolve()
    if not str(p).startswith(str((ROOT / "vendor").resolve())) or not p.exists():
        return jsonify({"error": "not found"}), 404
    return send_file(p)


@app.route("/api/graph")
def api_graph():
    """The pre-laid-out 3D knowledge graph. Built by build_neural_map.py â€”
    the layout solver runs there, never in the headset's frame budget."""
    if not NEURAL_MAP.exists():
        return jsonify({"error": "neural map not built",
                        "hint": "run: py -3.11 build_neural_map.py"}), 404
    return send_file(NEURAL_MAP, mimetype="application/json")


@app.route("/api/config")
def api_config():
    c = cfg()
    nv = nvidia_block(c)
    return jsonify({
        "states": [{"name": n, "top_k": s.get("top_k", 12)}
                   for n, s in (c.get("cognitive_states") or {}).items()],
        # rhf_nodes are the dream engine's lens/pathway nodes. Chat derives its
        # lens from the chosen cognitive_state rather than asking twice; this
        # is the fallback for states that have no same-named node.
        "nodes": sorted((c.get("rhf_nodes") or {}).keys()),
        "default_node": str((c.get("client_config") or {}).get("default_node", "lumos")),
        "nvidia": {"enabled": bool(nv.get("enabled")), "ready": nvidia_ready(c),
                   "model": str(nv.get("model", "")).strip()},
        "light_model": c.get("light_model", ""),
    })


@app.route("/api/toggle_nvidia", methods=["POST"])
def api_toggle_nvidia():
    enabled = bool((request.json or {}).get("enabled"))
    c = cfg()
    block = c.get("nvidia_api_config") or {}
    block["enabled"] = enabled
    c["nvidia_api_config"] = block
    CONFIG_PATH.write_text(json.dumps(c, indent=4, ensure_ascii=False), encoding="utf-8")
    return jsonify({"enabled": enabled, "ready": nvidia_ready(c)})


@app.route("/api/command", methods=["POST"])
def api_command():
    c = cfg()
    command = str((request.json or {}).get("command", "")).strip()
    try:
        if command.lower().startswith("/unlock "):
            r = http.post(f"{bridge(c)}/unlock_sigil",
                          json={"sigil_name": command.split(" ", 1)[1].strip()}, timeout=30)
        else:
            r = http.post(f"{bridge(c)}/command", json={"command": command}, timeout=30)
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"status": "error", "response": f"Bridge unreachable: {e}"})


@app.route("/api/state")
def api_state():
    c = cfg()
    out = {"ts": time.time()}

    try:
        r = http.get(f"{bridge(c)}/health", timeout=3)
        out["engine"] = r.json()
    except Exception:
        out["engine"] = {"status": "OFFLINE"}

    try:
        r = http.get(f"{bridge(c)}/stats", timeout=5)
        out["stats"] = r.json() if r.ok else None
    except Exception:
        out["stats"] = None

    hb = read_json(ROOT / "echo_heartbeat.json")
    out["echo"] = hb
    out["soul"] = read_json(ROOT / "grid_heartbeat.json")
    out["nvidia"] = {"enabled": bool(nvidia_block(c).get("enabled")), "ready": nvidia_ready(c)}

    # Dream feed: newest pings â€” pending in relay root + delivered in processed_pings.
    # The archive grows by ~360 pings/day, so stat everything (cheap) but only
    # open and parse the newest DREAM_FEED_LIMIT â€” otherwise this 5s poll would
    # end up re-parsing thousands of files a minute.
    candidates = []
    for folder, delivered in ((RELAY, False), (RELAY / "processed_pings", True)):
        if folder.exists():
            # Echo archives delivered pings as *.json.processing â€” accept both
            for f in folder.glob("ping_*.json*"):
                try:
                    candidates.append((f.stat().st_mtime, f, delivered))
                except OSError:
                    continue
    candidates.sort(key=lambda c: c[0], reverse=True)

    dreams = []
    for mtime, f, delivered in candidates[:DREAM_FEED_LIMIT]:
        d = read_json(f)
        if isinstance(d, dict):
            dreams.append({
                "file": f.name,
                "mtime": mtime,
                "delivered": delivered,
                "agent": d.get("agent_name", "?"),
                "urgency": d.get("urgency", ""),
                "subject": d.get("subject", ""),
                "source": d.get("source", ""),
                "mode": d.get("dream_mode", ""),
                "synthesis": (d.get("synthesis") or "")[:900],
                "seed": (d.get("seed_text") or "")[:300],
                "fragments": len(d.get("body_fragments") or []),
            })
    out["dreams"] = dreams
    out["dream_total"] = len(candidates)
    return jsonify(out)


AETHER_CACHE = {"ts": 0.0, "data": None}

# --- Gnostic Grimoire v7.1 bridge -----------------------------------------
# The Grimoire is the authoritative RHC seismic engine (geometric mean of the
# three components + self-scoring ledger). The deck reads its snapshot rather
# than re-deriving it; /api/aether stays as the fallback when no reading exists.
# Optional: set "grimoire_dir" in config.json to wherever Gnostic_Grimoire
# lives. Defaults to a sibling folder so the deck works out of the box.
GRIMOIRE_DIR = ROOT.parent / "astro timings"
GRIMOIRE_RUN = {"busy": False, "last": ""}
GRIMOIRE_LOCK = threading.Lock()


def _grim_paths():
    d = Path(cfg().get("grimoire_dir") or GRIMOIRE_DIR)
    return d, d / "Gnostic_Logs_v7" / "latest.json", d / "Gnostic_Logs_v7" / "rhc_seismic_ledger.jsonl"


@app.route("/api/grimoire")
def api_grimoire():
    _, latest, ledger_path = _grim_paths()
    snap = read_json(latest)
    if not snap:
        return jsonify({"status": "none", "running": GRIMOIRE_RUN["busy"], "error": GRIMOIRE_RUN["last"]})

    out = {"status": "ok", "running": GRIMOIRE_RUN["busy"], "error": GRIMOIRE_RUN["last"],
           "timestamp": snap.get("timestamp"), "location": (snap.get("location") or {}).get("name"),
           "rhc": snap.get("rhc"), "astro": snap.get("astro")}
    try:
        ts = datetime.fromisoformat(str(snap.get("timestamp")))
        out["age_min"] = round((datetime.now(ts.tzinfo) - ts).total_seconds() / 60)
    except Exception:
        out["age_min"] = None

    cosmic = snap.get("cosmic") or {}
    for k in ("kp", "wind", "mag", "xray", "schumann", "f107", "protons", "donki", "neo"):
        out[k] = cosmic.get(k)
    earth = snap.get("earth") or {}
    out["usgs"] = earth.get("usgs")
    out["gdacs"] = earth.get("gdacs")
    local = snap.get("local") or {}
    out["weather"] = local.get("weather")
    out["marine"] = local.get("marine")
    out["feeds"] = snap.get("feeds")

    # ledger tail: open watches + recent verdicts
    watches, resolutions = [], []
    if ledger_path.exists():
        try:
            for line in ledger_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                e = json.loads(line)
                (watches if e.get("type") == "watch" else resolutions).append(e)
        except Exception:
            pass
    resolved_ids = {r.get("watch_id") for r in resolutions}
    now_iso = datetime.now(timezone.utc).isoformat()
    out["open_watches"] = [w for w in watches
                           if w.get("id") not in resolved_ids and str(w.get("window_end", "")) > now_iso]
    out["recent_verdicts"] = resolutions[-5:]
    return jsonify(out)


@app.route("/api/grimoire/run", methods=["POST"])
def api_grimoire_run():
    """Fire a full morning reading in the background (network fan-out ~10-30s)."""
    d, _, _ = _grim_paths()
    script = d / "Gnostic_Grimoire_v7.1.py"
    if not script.exists():
        return jsonify({"status": "error", "message": f"not found: {script}"}), 404

    # Claim the slot synchronously â€” the Grimoire appends to an append-only
    # prediction ledger, so two concurrent runs would file duplicate watches
    # and corrupt the self-scoring record. Checking the flag and spawning the
    # thread must be atomic (a double-clicked button is enough to race it).
    with GRIMOIRE_LOCK:
        if GRIMOIRE_RUN["busy"]:
            return jsonify({"status": "busy"})
        GRIMOIRE_RUN["busy"] = True
        GRIMOIRE_RUN["last"] = ""

    def _run():
        try:
            r = subprocess.run([sys.executable, str(script), "--no-color"],
                               cwd=str(d), capture_output=True, text=True, timeout=240)
            # exit 2 is the script's "discharge watch active" signal, not a failure
            if r.returncode not in (0, 2):
                GRIMOIRE_RUN["last"] = (r.stderr or r.stdout or "unknown error").strip()[-300:]
        except Exception as e:
            GRIMOIRE_RUN["last"] = f"{type(e).__name__}: {e}"[:300]
        finally:
            GRIMOIRE_RUN["busy"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/aether")
def api_aether():
    """Live space-weather + tectonic feeds (free, keyless where possible),
    plus the deck's operationalization of the rhc_seismic_engine axiom:
    SeismicRisk = f(CME_I Ã— Atm_P Ã— Crustal_S). Cached 5 min."""
    if AETHER_CACHE["data"] and time.time() - AETHER_CACHE["ts"] < 300:
        return jsonify(AETHER_CACHE["data"])

    out = {"ts": time.time()}

    def grab(url, timeout=6):
        try:
            r = http.get(url, timeout=timeout)
            return r.json() if r.ok else None
        except Exception:
            return None

    # NOAA SWPC â€” solar wind plasma + magnetic field + Kp + GOES X-ray.
    # Every block guards itself: SWPC intermittently serves error objects or
    # non-numeric cells, and one bad feed must not take down the whole reading
    # (quakes, NEOs and the seismic composite are computed further down).
    plasma = grab("https://services.swpc.noaa.gov/products/solar-wind/plasma-7-day.json")
    if isinstance(plasma, list) and len(plasma) > 1:
        try:
            row = plasma[-1]
            out["sw_density"] = float(row[1]) if row[1] else None
            out["sw_speed"] = float(row[2]) if row[2] else None
        except (TypeError, ValueError, IndexError, KeyError):
            pass
    mag = grab("https://services.swpc.noaa.gov/products/solar-wind/mag-7-day.json")
    if mag and len(mag) > 1:
        try:
            out["bz"] = float(mag[-1][3])
        except Exception:
            pass
    kp = grab("https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json")
    if kp and len(kp) > 1:
        try:
            out["kp"] = float(kp[-1][1])
        except Exception:
            pass
    xr = grab("https://services.swpc.noaa.gov/json/goes/primary/xrays-6-hour.json")
    if xr:
        try:
            flux = [e["flux"] for e in xr if e.get("energy") == "0.1-0.8nm"][-1]
            out["xray_flux"] = flux
            import math as _m
            for cls, th in (("X", 1e-4), ("M", 1e-5), ("C", 1e-6), ("B", 1e-7)):
                if flux >= th:
                    out["xray_class"] = f"{cls}{flux/th:.1f}"
                    break
            else:
                out["xray_class"] = "A"
        except Exception:
            pass

    # USGS quakes (last 24h, M2.5+)
    q = grab("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson")
    if isinstance(q, dict) and q.get("features"):
        try:
            feats = [f for f in q["features"] if isinstance(f.get("properties"), dict)]
            out["quake_count"] = len(feats)
            if feats:
                def _mag(f):
                    return f["properties"].get("mag") or 0
                big = max(feats, key=_mag)
                out["quake_max"] = big["properties"].get("mag")
                out["quake_place"] = (big["properties"].get("place") or "")[:48]
                # Loaded zones: strongest recent events = candidate discharge segments
                out["quake_zones"] = [
                    {"place": (f["properties"].get("place") or "?")[:44],
                     "mag": f["properties"].get("mag")}
                    for f in sorted(feats, key=_mag, reverse=True)[:3]]
        except (TypeError, ValueError, KeyError, AttributeError):
            pass

    # NASA NeoWs (DEMO_KEY is fine at our 5-min cache rate)
    neo = grab("https://api.nasa.gov/neo/rest/v1/feed/today?detailed=false&api_key=DEMO_KEY", timeout=8)
    if isinstance(neo, dict) and neo.get("near_earth_objects"):
        objs = [o for day in neo["near_earth_objects"].values() for o in day]
        out["neo_count"] = len(objs)
        try:
            closest = min(objs, key=lambda o: float(o["close_approach_data"][0]["miss_distance"]["lunar"]))
            out["neo_closest_ld"] = round(float(closest["close_approach_data"][0]["miss_distance"]["lunar"]), 1)
            out["neo_name"] = closest.get("name", "")[:22]
            out["neo_hazard"] = any(o.get("is_potentially_hazardous_asteroid") for o in objs)
        except Exception:
            pass

    # RHC seismic engine â€” deck operationalization of the axiom (f is not
    # specified in the corpus; this normalized multiplicative blend is ours)
    try:
        import math as _m
        v = out.get("sw_speed") or 380.0
        bz = out.get("bz") or 0.0
        flux = out.get("xray_flux") or 1e-8
        kpv = out.get("kp") or 2.0
        mmax = out.get("quake_max") or 4.0
        cme = max(0.0, min(1.0, (v - 300) / 500)) * 0.5 \
            + max(0.0, min(1.0, -bz / 15)) * 0.3 \
            + max(0.0, min(1.0, (_m.log10(max(flux, 1e-9)) + 8) / 4)) * 0.2
        atm = max(0.0, min(1.0, kpv / 9))
        crust = max(0.0, min(1.0, mmax / 8))
        out["seismic_risk"] = round(100 * cme * (0.4 + 0.6 * atm) * (0.4 + 0.6 * crust), 1)
        out["cme_i"], out["atm_p"], out["crustal_s"] = round(cme, 3), round(atm, 3), round(crust, 3)
    except Exception:
        pass

    AETHER_CACHE["data"] = out
    AETHER_CACHE["ts"] = time.time()
    return jsonify(out)


@app.route("/api/chat", methods=["POST"])
def api_chat():
    body = request.json or {}
    message = str(body.get("message", "")).strip()
    state_name = str(body.get("state", "")).strip()
    node = str(body.get("node", "lumos")).strip()
    use_memory = bool(body.get("use_memory", True))
    if not message:
        return jsonify({"error": "empty message"}), 400

    c = cfg()
    states = c.get("cognitive_states") or {}
    # Never silently substitute a persona: answering as Lumos when Veritas was
    # asked for would be indexed into memory under the wrong attribution.
    if state_name and state_name not in states:
        return jsonify({"error": f"unknown cognitive state '{state_name}'",
                        "available": sorted(states.keys())}), 400
    state = states.get(state_name) or next(iter(states.values()), {})
    system_prompt = str(state.get("system_prompt", "You are a node of the Awen Grid."))
    top_k = int(state.get("top_k", 12))

    cloud = nvidia_ready(c)
    cconf = c.get("client_config") or {}
    if cloud:
        max_items = int(cconf.get("max_memory_items_cloud", 24))
    else:
        max_items = int(cconf.get("max_memory_items_local", 12))
    max_chars = int(cconf.get("memory_chunk_max_chars", 700))

    mem_lines, mem_count = [], 0
    if use_memory:
        try:
            r = http.post(f"{bridge(c)}/search",
                          json={"query": message, "node": node, "params": {"top_k": top_k}},
                          timeout=45)
            hits = r.json() if r.ok else []
            for i, m in enumerate([h for h in hits if isinstance(h, dict) and h.get("source") != "error"][:max_items], 1):
                chunk = str(m.get("chunk", "")).replace("\n", " ").strip()[:max_chars]
                mem_lines.append(f"[{i}] ({m.get('source','')}) {chunk}")
            mem_count = len(mem_lines)
        except Exception:
            pass

    user_prompt = message
    if mem_lines:
        user_prompt = f"{message}\n\nRESONANT MEMORIES:\n" + "\n".join(mem_lines)

    # LLM call: NVIDIA when armed, else LM Studio
    try:
        if cloud:
            nv = nvidia_block(c)
            model_label = "â˜ " + str(nv.get("model", "")).strip()
            r = http.post(str(nv.get("base_url", "https://integrate.api.nvidia.com/v1")).rstrip("/") + "/chat/completions",
                          headers={"Authorization": f"Bearer {str(nv.get('api_key','')).strip()}"},
                          json={"model": str(nv.get("model", "")).strip(),
                                "messages": [{"role": "system", "content": system_prompt},
                                             {"role": "user", "content": user_prompt}],
                                "temperature": float(c.get("lmstudio_temp", 0.6)),
                                "max_tokens": int(nv.get("max_tokens", 4096))},
                          timeout=int(nv.get("timeout", 300)))
        else:
            model = str(c.get("light_model") or c.get("deep_model") or "").strip()
            model_label = "ðŸ  " + model
            payload = {"model": model,
                       "messages": [{"role": "system", "content": system_prompt},
                                    {"role": "user", "content": user_prompt}],
                       "temperature": float(c.get("lmstudio_temp", 0.6))}
            mt = int(c.get("lmstudio_max_tokens", -1))
            if mt > 0:
                payload["max_tokens"] = mt
            r = http.post(str(c.get("lmstudio_url", "http://localhost:1234")).rstrip("/") + "/v1/chat/completions",
                          json=payload, timeout=int(c.get("lmstudio_timeout", 800)))
        r.raise_for_status()
        content = r.json()["choices"][0]["message"].get("content") or ""
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        if not content:
            content = "(empty response â€” model may have spent its budget thinking)"
    except Exception as e:
        return jsonify({"response": f"âš  LLM call failed: {e}", "model": "", "mem_count": mem_count, "indexed": False})

    # Index the exchange as ONE entry: question and answer stay together, so a
    # dream that later surfaces this fragment still knows what was asked.
    # (Saving replies alone leaves orphaned answers with no context.)
    indexed = False
    if bool(cconf.get("index_chat", True)):
        cap = int(cconf.get("chat_entry_max_chars", 1800))
        q = message if len(message) <= cap else message[:cap] + " â€¦"
        a = content if len(content) <= cap else content[:cap] + " â€¦"
        entry = f"CHAT ({state_name or 'node'} Â· {node})\nQ: {q}\n\nA: {a}"
        try:
            r = http.post(f"{bridge(c)}/add_entry",
                          json={"text": entry, "profile": "private", "node": node,
                                "source": f"Awen Command Deck ({node})"}, timeout=60)
            indexed = r.ok and r.json().get("status") == "success"
        except Exception:
            pass

    return jsonify({"response": content, "model": model_label, "mem_count": mem_count, "indexed": indexed})


if __name__ == "__main__":
    # Loopback by default. --lan binds every interface so a standalone headset
    # on the same wifi can reach the VR room â€” only do that on a network you
    # trust, since the deck proxies an unauthenticated memory API.
    lan = "--lan" in sys.argv
    host = "0.0.0.0" if lan else "127.0.0.1"

    print("=" * 62)
    print("  ðŸœ‚ðŸœðŸœƒðŸœ„  AWEN GRID â€” COMMAND DECK")
    print(f"  deck : http://localhost:{PORT}")
    print(f"  vr   : http://localhost:{PORT}/vr")
    if lan:
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close()
            print(f"  lan  : http://{ip}:{PORT}/vr   â† from the headset browser")
        except Exception:
            print("  lan  : bound to all interfaces")
        print("  âš  LAN mode: anyone on this network can reach the deck.")
    print("=" * 62)
    try:
        from waitress import serve
        serve(app, host=host, port=PORT, threads=8)
    except ImportError:
        app.run(host=host, port=PORT, debug=False)

