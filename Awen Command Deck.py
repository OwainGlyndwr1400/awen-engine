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


# --- the dream explorer ----------------------------------------------------
DREAMS_HTML = ROOT / "awen_dreams.html"
_DREAMS_CACHE = {"ts": 0.0, "data": None}


@app.route("/dreams")
def dreams_page():
    return send_file(DREAMS_HTML)


@app.route("/api/dreams")
def api_dreams():
    """The FULL dream archive, not the feed's newest 20 — every ping in the
    relay and the processed archive, with complete synthesis, seed and
    fragments, so a dream can actually be read rather than glimpsed before
    the next poll wipes it. Cached 60s; the archive only grows."""
    if _DREAMS_CACHE["data"] and time.time() - _DREAMS_CACHE["ts"] < 60:
        return jsonify(_DREAMS_CACHE["data"])
    out = []
    for d, delivered in ((RELAY, False), (RELAY / "processed_pings", True)):
        if not d.exists():
            continue
        for f in d.glob("ping_*.json"):
            j = read_json(f)
            if not isinstance(j, dict):
                continue
            try:
                score = int(str(j.get("urgency", "0/0")).split("/")[0])
            except (ValueError, IndexError):
                score = 0
            try:
                ts_ = int(f.stem.split("_")[-1])
            except ValueError:
                ts_ = int(f.stat().st_mtime)
            src = str(j.get("source", ""))
            out.append({
                "id": str(j.get("subject", "")).replace("DreamID:", "").strip() or f.stem,
                "agent": str(j.get("agent_name", "")),
                "urgency": score,
                "lane": "knowledge" if src in ("private", "knowledge") else (src or "?"),
                "mode": str(j.get("dream_mode") or "classic"),
                "ts": ts_,
                "delivered": delivered,
                "seed": str(j.get("seed_text", ""))[:1500],
                "fragments": [str(x)[:1500] for x in (j.get("body_fragments") or [])],
                "synthesis": str(j.get("synthesis", "")),
                "synthesis_error": j.get("synthesis_error"),
                "backfilled": j.get("synthesis_backfilled"),
            })
    out.sort(key=lambda x: -x["ts"])
    payload = {"count": len(out), "dreams": out}
    _DREAMS_CACHE["data"], _DREAMS_CACHE["ts"] = payload, time.time()
    return jsonify(payload)


PAPERS_HTML = ROOT / "awen_papers.html"
PAPERS = ROOT / "docs" / "papers.json"


@app.route("/papers")
def papers_page():
    return send_file(PAPERS_HTML)


@app.route("/api/papers")
def api_papers():
    """The published bibliography. Unlike the atlas, this artifact is derived
    entirely from public work and is safe to ship — build_papers.py scrubs the
    operator's tunnel host and drive links on the way out."""
    if not PAPERS.exists():
        return jsonify({"error": "library not built",
                        "hint": "run: py -3.11 build_papers.py"}), 404
    return send_file(PAPERS, mimetype="application/json")



# --- RHC seismic forecast -------------------------------------------------
# The axiom's own execution clause: peak capacity forces redistribution inside
# a 32-72h window. The charge term is global and says WHETHER; the regional
# term says WHERE. Both live in rhc_seismic_forecast.py -- imported rather than
# reimplemented, because the deck already carried one rival implementation of
# this axiom and that is exactly how two different answers appeared.
_FORECAST_CACHE = {"ts": 0.0, "data": None}


@app.route("/api/seismic_forecast")
def api_seismic_forecast():
    if _FORECAST_CACHE["data"] and time.time() - _FORECAST_CACHE["ts"] < 900:
        return jsonify(_FORECAST_CACHE["data"])
    try:
        import importlib
        fc = importlib.import_module("rhc_seismic_forecast")
        importlib.reload(fc)
        st = fc.charge_state()
        out = {
            "charge": st.get("charge"), "cme_i": st.get("cme_i"),
            "atm_p": st.get("atm_p"), "gate": fc.CHARGE_GATE,
            "charged": (st.get("charge") or 0) >= fc.CHARGE_GATE,
            "degraded": st.get("degraded"), "live_inputs": st.get("live_inputs"),
            "window_h": fc.WINDOW_H, "shallow_km": fc.SHALLOW_KM,
            "targets": [], "baseline": None,
        }
        regions, _total = fc.regional_state()
        if regions:
            ranked = sorted(regions.values(), key=fc.readiness, reverse=True)
            top = [r for r in ranked if r.get("m_expected")][:fc.TOP_N]
            base = max(regions.values(), key=lambda r: r["events_30d"])
            out["baseline"] = {"label": base["label"], "events_30d": base["events_30d"]}
            for r in top:
                m_low = round(r["m_expected"] - 0.5, 2)
                out["targets"].append({
                    "label": r["label"], "cell": r["cell"],
                    "m_low": m_low, "m_high": round(r["m_expected"] + 0.5, 2),
                    "readiness": fc.readiness(r),
                    "strain_deficit": r.get("strain_deficit"),
                    "shallow_share": r.get("shallow_share"),
                    "prior_p": fc.prior_probability(r["events_30d"], r["b_value"],
                                                    r["mc"], m_low, 30, fc.WINDOW_H),
                })
        # "Keep an eye out" only works if a genuinely charged state leaves a
        # scoreable record even when nobody is at a terminal. One open window
        # at a time; the CLI's `score` command closes it after 72h. Forced and
        # degraded states never persist — those are not measurements.
        if out["charged"] and not out.get("degraded") and regions:
            try:
                if not fc.has_open_forecast():
                    rec = fc.build_forecast(st, regions, _total,
                                            forced=False, issued_by="deck")
                    if rec:
                        fc.write_forecast(rec)
                        out["issued_id"] = rec["id"]
            except Exception:
                pass

        _FORECAST_CACHE["data"], _FORECAST_CACHE["ts"] = out, time.time()
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": f"forecast failed: {e}"}), 500


ATLAS = ROOT / "docs" / "atlas.json"


@app.route("/api/atlas")
def api_atlas():
    """Clusters of the LIVE FAISS index — the memory the engine actually dreams
    over, as opposed to /api/graph which is the graphify document catalogue.
    Built by build_atlas.py."""
    if not ATLAS.exists():
        return jsonify({"error": "atlas not built",
                        "hint": "run: py -3.11 build_atlas.py"}), 404
    return send_file(ATLAS, mimetype="application/json")


@app.route("/api/probe", methods=["POST"])
def api_probe():
    """Run a real retrieval and report which ATLAS clusters lit up.

    This is what drives the Neural Map's activation flash: ask a question, watch
    the regions of the living memory that answer it fire. The engine tags every
    hit with its cluster (see build_atlas.py), so nothing is inferred here.
    """
    body = request.get_json(silent=True) or {}
    query = str(body.get("query", "")).strip()
    if not query:
        return jsonify({"error": "query required"}), 400
    node = str(body.get("node", "lumos")).strip() or "lumos"
    try:
        top_k = max(1, min(50, int(body.get("top_k", 14))))
    except (TypeError, ValueError):
        top_k = 14

    c = cfg()
    try:
        r = requests.post(f"{bridge(c)}/search",
                          json={"query": query, "node": node,
                                "params": {"top_k": top_k}}, timeout=90)
        r.raise_for_status()
        hits = r.json()
    except Exception as e:
        return jsonify({"error": f"engine unreachable: {e}"}), 503
    if isinstance(hits, dict) and hits.get("error"):
        return jsonify({"error": hits["error"]}), 400

    fired, order = {}, []
    for h in hits:
        cid = h.get("cluster")
        if not cid:
            continue
        if cid not in fired:
            fired[cid] = {"cluster": cid, "hits": 0,
                          "best": h.get("distance"), "lane": h.get("source")}
            order.append(cid)
        fired[cid]["hits"] += 1
        if h.get("distance") is not None and h["distance"] < fired[cid]["best"]:
            fired[cid]["best"] = h["distance"]

    return jsonify({
        "query": query, "node": node, "returned": len(hits),
        "clusters": [fired[c_] for c_ in order],
        "untagged": sum(1 for h in hits if not h.get("cluster")),
        "excerpts": [{"cluster": h.get("cluster"), "lane": h.get("source"),
                      "d": h.get("distance"),
                      "text": " ".join(str(h.get("chunk", "")).split())[:260]}
                     for h in hits[:8]],
    })


REGULUS = ROOT / "docs" / "regulus_corridor.json"


@app.route("/api/regulus")
def api_regulus():
    """Declination of Regulus (and four control stars) per epoch, precomputed by
    build_regulus_corridor.py with astropy. The panel does only the spherical
    triangle in JS, so what it shows is computed astronomy, not stored text."""
    if not REGULUS.exists():
        return jsonify({"error": "corridor not built",
                        "hint": "run: py -3.11 build_regulus_corridor.py"}), 404
    return send_file(REGULUS, mimetype="application/json")


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
                "synthesis_error": d.get("synthesis_error"),
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


def _predictions(grim_dir: Path):
    """The open predictions board — falsifiable claims with their kill
    conditions. Shared artefact: the Grimoire renders it each morning, the
    deck shows the same file. Not a gauge; a scoreboard."""
    p = grim_dir / "predictions.json"
    d = read_json(p) or {}
    out = []
    for pr in d.get("predictions") or []:
        out.append({k: pr.get(k) for k in
                    ("id", "title", "value", "observe", "falsifies_if", "scale", "status", "countdown")})
    return out


@app.route("/api/grimoire")
def api_grimoire():
    grim_dir, latest, ledger_path = _grim_paths()
    snap = read_json(latest)
    if not snap:
        return jsonify({"status": "none", "running": GRIMOIRE_RUN["busy"],
                        "error": GRIMOIRE_RUN["last"], "predictions": _predictions(grim_dir)})

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
    out["predictions"] = _predictions(grim_dir)
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
    # SWPC retired products/solar-wind/plasma-*.json (404 now). The summary
    # endpoint is the live equivalent: [{"proton_speed", "time_tag"}].
    plasma = grab("https://services.swpc.noaa.gov/products/summary/solar-wind-speed.json")
    if isinstance(plasma, list) and plasma:
        try:
            out["sw_speed"] = float(plasma[-1]["proton_speed"])
        except (KeyError, ValueError, TypeError):
            pass
    # Same retirement for mag-*.json; summary gives {"bt", "bz_gsm"}.
    mag = grab("https://services.swpc.noaa.gov/products/summary/solar-wind-mag-field.json")
    if isinstance(mag, list) and mag:
        try:
            out["bz"] = float(mag[-1]["bz_gsm"])
        except (KeyError, ValueError, TypeError):
            pass
    # This feed changed shape: it returns [{"time_tag", "Kp", ...}], not the
    # list-of-lists it once did. The old index access raised KeyError and the
    # bare except left Kp pinned to its 2.0 default -- during a real G1 storm
    # the panel read 22% instead of 59%. Accept both shapes.
    kp = grab("https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json")
    if isinstance(kp, list) and kp:
        try:
            last = kp[-1]
            out["kp"] = float(last["Kp"] if isinstance(last, dict) else last[1])
        except (KeyError, ValueError, TypeError, IndexError):
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

    # NASA NeoWs. DEMO_KEY is limited to 30 requests/hour and 50/day PER IP —
    # not per key — so it is shared with every other app on this machine that
    # also falls back to it. A free registered key raises that to 1,000/hour.
    # Read from config so the key never sits in source or in the repo.
    nasa_key = str(cfg().get("nasa_api_key", "")).strip() or "DEMO_KEY"
    neo = grab("https://api.nasa.gov/neo/rest/v1/feed/today?detailed=false"
               f"&api_key={nasa_key}", timeout=8)
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

    # RHC seismic engine â€” the deck no longer computes one. It used to carry
    # its own blend here (no Schumann, no lunar phase, a single global max
    # magnitude for Crustal_S), which was a DIFFERENT function from the
    # Grimoire's, so one axiom showed two numbers depending on which code path
    # painted the panel last. One implementation now: the Grimoire's. The deck
    # relays its latest snapshot, stamped with its age, and says "no reading"
    # rather than inventing a rival figure.
    try:
        _, latest, _ = _grim_paths()
        snap = read_json(latest)
        eng = (((snap or {}).get("rhc") or {}).get("engine")
               or (snap or {}).get("engine") or {})
        if all(k in eng for k in ("cme_i", "atm_p", "crustal_s", "gridload")):
            out["cme_i"] = eng["cme_i"]
            out["atm_p"] = eng["atm_p"]
            out["crustal_s"] = eng["crustal_s"]
            out["seismic_risk"] = eng["gridload"]
            out["band"] = eng.get("band")
            out["seismic_source"] = "grimoire"
            try:
                out["seismic_age_s"] = int(time.time() - latest.stat().st_mtime)
            except OSError:
                pass
        else:
            out["seismic_source"] = "none"
        # Feed liveness still reported â€” it describes the raw readouts above.
        live = [k for k in ("sw_speed", "bz", "kp", "xray_flux") if k in out]
        out["seismic_inputs_live"] = live
        out["seismic_degraded"] = len(live) < 3
    except Exception:
        out["seismic_source"] = "none"

    AETHER_CACHE["data"] = out
    AETHER_CACHE["ts"] = time.time()
    return jsonify(out)


# ===========================================================================
#  TOOLS — the Circle could only ever talk. Now it can look things up.
#
#  OpenAI-compatible `tool_calls`, run in a bounded loop (default 4 rounds)
#  before the model is made to answer. Every tool is read-mostly and sandboxed:
#
#   * read_file NEVER leaves the project directory, and refuses config.json and
#     anything else secret-bearing — config.json holds a Gmail app password.
#   * write_note can only write inside notes/, and only .md.
#   * search_memory goes through the engine, so it inherits the node's role
#     (non-admin nodes see shared only) and the split-lane quotas.
#
#  Nothing here can delete, execute, or reach the network unless a search key
#  is explicitly configured.
# ===========================================================================
NOTES_DIR = ROOT / "notes"
SECRET_NAMES = {"config.json", ".env", "credentials.json", "token.json", "secrets.json"}
READABLE_SUFFIXES = {".md", ".txt", ".json", ".py", ".html", ".css", ".js", ".jsonl", ".csv", ".yml", ".yaml"}

TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "search_memory",
        "description": "Search the Awen Grid's own memory (the FAISS archive of research, "
                       "books and past exchanges). Use this whenever you need to check what "
                       "the Grid actually holds rather than relying on your own recall.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "What to look for."},
            "top_k": {"type": "integer", "description": "How many chunks (1-20, default 8)."}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a text file from the Awen Grid project directory. "
                       "Use for the framework docs, glossary, theorem indices, scripts.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Path relative to the project root, e.g. 'RHC_FRAMEWORK.md'."},
            "max_chars": {"type": "integer", "description": "Truncate to this many characters (default 6000)."}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "list_files",
        "description": "List files in the project directory, optionally filtered by a glob.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Glob such as '*.md' or 'docs/*.json'. Default '*.md'."}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "write_note",
        "description": "Save a note to the Grid's notes/ folder so it persists beyond this conversation.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "Short title; becomes the filename."},
            "text": {"type": "string", "description": "Markdown body of the note."}},
            "required": ["title", "text"]}}},
    {"type": "function", "function": {
        "name": "current_time",
        "description": "The current local date and time.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
]


def _safe_project_path(rel: str) -> Path:
    """Resolve `rel` inside ROOT or raise. Blocks traversal and secrets."""
    p = (ROOT / str(rel).strip().lstrip("/\\")).resolve()
    if not str(p).startswith(str(ROOT.resolve())):
        raise ValueError("path escapes the project directory")
    if p.name.lower() in SECRET_NAMES:
        raise ValueError(f"'{p.name}' is not readable through tools (it holds credentials)")
    return p


def run_tool(name: str, args: dict, c: dict, node: str) -> str:
    """Execute one tool call. Always returns a string — the model reads it."""
    try:
        if name == "search_memory":
            q = str(args.get("query", "")).strip()
            if not q:
                return "error: query is required"
            k = max(1, min(20, int(args.get("top_k", 8) or 8)))
            r = requests.post(f"{bridge(c)}/search",
                              json={"query": q, "node": node, "params": {"top_k": k}}, timeout=90)
            r.raise_for_status()
            hits = r.json()
            if not isinstance(hits, list) or not hits:
                return "no matches in memory"
            out = []
            for i, h in enumerate(hits, 1):
                txt = " ".join(str(h.get("chunk", "")).split())[:700]
                out.append(f"[{i}] ({h.get('source','?')}, d={h.get('distance',0):.3f}) {txt}")
            return "\n".join(out)

        if name == "read_file":
            p = _safe_project_path(args.get("path", ""))
            if not p.exists() or not p.is_file():
                return f"error: '{args.get('path')}' not found"
            if p.suffix.lower() not in READABLE_SUFFIXES:
                return f"error: '{p.suffix}' is not a readable text type"
            cap = max(200, min(20000, int(args.get("max_chars", 6000) or 6000)))
            txt = p.read_text(encoding="utf-8", errors="replace")
            return txt[:cap] + ("\n… (truncated)" if len(txt) > cap else "")

        if name == "list_files":
            pat = str(args.get("pattern", "*.md")).strip() or "*.md"
            if ".." in pat:
                return "error: '..' not allowed"
            found = sorted(p for p in ROOT.glob(pat)
                           if p.is_file() and p.name.lower() not in SECRET_NAMES)
            if not found:
                return f"no files match '{pat}'"
            return "\n".join(f"{p.relative_to(ROOT)}  ({p.stat().st_size/1024:.1f} KB)"
                             for p in found[:120])

        if name == "write_note":
            title = str(args.get("title", "")).strip() or "untitled"
            slug = re.sub(r"[^a-zA-Z0-9\- ]+", "", title).strip().replace(" ", "-")[:60] or "untitled"
            NOTES_DIR.mkdir(exist_ok=True)
            p = NOTES_DIR / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{slug}.md"
            p.write_text(f"# {title}\n\n{args.get('text','')}\n", encoding="utf-8")
            return f"saved: notes/{p.name}"

        if name == "current_time":
            return datetime.now().strftime("%A %d %B %Y, %H:%M:%S (local)")

        return f"error: unknown tool '{name}'"
    except Exception as e:
        return f"error: {e}"


def llm_call(c: dict, cloud: bool, messages: list, tools=None, timeout=None):
    """One OpenAI-compatible completion against NVIDIA or LM Studio."""
    if cloud:
        nv = nvidia_block(c)
        payload = {"model": str(nv.get("model", "")).strip(), "messages": messages,
                   "temperature": float(c.get("lmstudio_temp", 0.6)),
                   "max_tokens": int(nv.get("max_tokens", 4096))}
        if tools:
            payload["tools"] = tools
        r = http.post(str(nv.get("base_url", "https://integrate.api.nvidia.com/v1")).rstrip("/") + "/chat/completions",
                      headers={"Authorization": f"Bearer {str(nv.get('api_key','')).strip()}"},
                      json=payload, timeout=timeout or int(nv.get("timeout", 300)))
    else:
        payload = {"model": str(c.get("light_model") or c.get("deep_model") or "").strip(),
                   "messages": messages, "temperature": float(c.get("lmstudio_temp", 0.6))}
        mt = int(c.get("lmstudio_max_tokens", -1))
        if mt > 0:
            payload["max_tokens"] = mt
        if tools:
            payload["tools"] = tools
        r = http.post(str(c.get("lmstudio_url", "http://localhost:1234")).rstrip("/") + "/v1/chat/completions",
                      json=payload, timeout=timeout or int(c.get("lmstudio_timeout", 800)))
    r.raise_for_status()
    return r.json()["choices"][0]["message"]


@app.route("/api/tools")
def api_tools():
    """What the Circle can reach for. Handy for the deck to display."""
    c = cfg()
    enabled = bool((c.get("client_config") or {}).get("tools_enabled", True))
    return jsonify({"enabled": enabled,
                    "tools": [{"name": t["function"]["name"],
                               "description": t["function"]["description"]}
                              for t in TOOL_SPECS]})


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

    # LLM call: NVIDIA when armed, else LM Studio.
    # With tools on this is a BOUNDED loop: the model may call tools, read the
    # results and call again, up to tool_max_rounds, after which it must answer
    # from what it has. Without the bound a confused model will call search_memory
    # forever and never speak.
    model_label = (("\u2601 " + str(nvidia_block(c).get("model", "")).strip()) if cloud
                   else ("\U0001f3e0 " + str(c.get("light_model") or c.get("deep_model") or "").strip()))
    use_tools = bool(cconf.get("tools_enabled", True))
    max_rounds = max(0, min(8, int(cconf.get("tool_max_rounds", 4))))

    # The persona prompts are 14 KB character briefs that never mention tools.
    # Passing `tools` alone is not enough: measured, every local model answered
    # from the persona and one of them CLAIMED to have read a file it never
    # opened. The affordance has to be stated in the system prompt or it does
    # not exist as far as the model is concerned.
    sys_content = system_prompt
    if use_tools:
        sys_content += (
            "\n\n---\nTOOLS. You have real tools: search_memory, read_file, "
            "list_files, write_note, current_time. They reach the Grid's actual "
            "FAISS archive and project files.\n"
            "- Prefer a tool over recall for anything checkable: what a document "
            "says, what the archive holds, today's date.\n"
            "- Never claim to have used a tool you did not call, and never invent "
            "a file's contents. If a tool errors, say so plainly.\n"
            "- Stay in character while using them; the tools serve the voice, not "
            "the other way round."
        )

    messages = [{"role": "system", "content": sys_content},
                {"role": "user", "content": user_prompt}]
    tool_trace = []

    try:
        msg = llm_call(c, cloud, messages, tools=TOOL_SPECS if use_tools else None)

        rounds = 0
        while use_tools and msg.get("tool_calls") and rounds < max_rounds:
            rounds += 1
            messages.append({"role": "assistant", "content": msg.get("content") or "",
                             "tool_calls": msg["tool_calls"]})
            for call in msg["tool_calls"]:
                fn = call.get("function") or {}
                name = str(fn.get("name", ""))
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                result = str(run_tool(name, args, c, node))
                tool_trace.append({"tool": name, "args": args,
                                   "ok": not result.startswith("error:"),
                                   "chars": len(result)})
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                 "name": name, "content": result[:8000]})
            msg = llm_call(c, cloud, messages, tools=TOOL_SPECS)

        if use_tools and msg.get("tool_calls") and rounds >= max_rounds:
            messages.append({"role": "user",
                             "content": "Tool budget spent. Answer now from what you have."})
            msg = llm_call(c, cloud, messages, tools=None)

        content = msg.get("content") or ""
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        if not content:
            content = "(empty response \u2014 model may have spent its budget thinking)"
    except Exception as e:
        return jsonify({"response": f"\u26a0 LLM call failed: {e}", "model": "",
                        "mem_count": mem_count, "indexed": False, "tools": tool_trace})

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
                          json={"text": entry, "profile": "conversations", "node": node,
                                "source": f"Awen Command Deck ({node})"}, timeout=60)
            indexed = r.ok and r.json().get("status") == "success"
        except Exception:
            pass

    return jsonify({"response": content, "model": model_label, "mem_count": mem_count,
                    "indexed": indexed, "tools": tool_trace})


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

