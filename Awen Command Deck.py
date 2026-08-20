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

import ast
import ipaddress
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from flask import Flask, Response, jsonify, request, send_file

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DECK_HTML = ROOT / "awen_deck.html"
RELAY = ROOT / "cognitive_relay"
PORT = int(os.environ.get("DECK_PORT", 7777))   # override for test instances
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


# --- the akashic codex ------------------------------------------------------
# One card per published theorem: the equation, what it claims, and the two
# honest cross-checks this project can actually make — the AUDIT verdict
# (does the equation evaluate / does the empirical claim hold, from the
# three-leg triangulation in Aether Scope's AUDIT.md) and the RUNTIME verdict
# (has the dream engine independently surfaced it — runtime_vs_spec.json).
# Verdicts join by name AND equation: the audit's ✗ on the Paper 11 Null
# Ledger form must never land on the Paper 2 row whose equation is fine.
CODEX_HTML = ROOT / "awen_codex.html"
THEOREM_INDEX = ROOT / "Memory" / "Tec_Obsidian" / "01_theorem_index.md"
RUNTIME_VS_SPEC = ROOT / "docs" / "runtime_vs_spec.json"
_CODEX_CACHE = {"ts": 0.0, "data": None}


def _norm_name(s: str) -> str:
    s = re.sub(r"\[\[|\]\]", "", str(s))
    s = re.sub(r"\(.*?\)", " ", s)          # parenthetical alt-titles
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _norm_eq(s: str) -> str:
    """ASCII skeleton for MATCHING (never display): unicode operators and
    markdown escaping vary between the index and AUDIT.md (\\| vs |, √ eaten
    by encoding), but the ascii structure of a formula survives — and it still
    separates true variants like 2/(1+i) vs (1+i)/2."""
    s = str(s).replace("−", "-").replace("×", "*").replace("·", "*").replace("\\", "")
    s = re.sub(r"[^\x20-\x7e]", "", s)
    return re.sub(r"\s+", "", s).lower()


def _parse_theorem_table() -> list:
    rows = []
    if not THEOREM_INDEX.exists():
        return rows
    for line in THEOREM_INDEX.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| [["):
            continue
        # cells may contain escaped pipes (bra-ket |Ψ> renders as \|)
        parts = [p.strip().replace("\\|", "|")
                 for p in re.split(r"(?<!\\)\|", line)][1:-1]
        if len(parts) < 7:
            continue
        m = re.search(r"\[\[(.+?)\]\]", parts[0])
        name = (m.group(1) if m else parts[0]).strip()
        clean = lambda x: re.sub(r"\[\[|\]\]", "", x)
        rows.append({"name": name,
                     "equation": clean(parts[1]), "significance": clean(parts[2]),
                     "derivation": clean(parts[3]), "validation": clean(parts[4]),
                     "application": clean(parts[5]), "paper": parts[6]})
    return rows


def _parse_audit(c: dict) -> dict:
    """AUDIT.md → {normalized name: [{verdict, paper, eq, notes}]}.
    Missing file is fine — cards render 'unaudited'."""
    p = Path(c.get("audit_md") or (ROOT.parent / "Aether Scope 4.0" / "AUDIT.md"))
    out = {}
    if not p.exists():
        return out
    tier, cur = None, None
    tiers = {"✗": "broken", "⚠": "check", "✓": "solid"}
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        mt = re.match(r"##\s+Tier\s+(✗|⚠|✓)", line)
        if mt:
            tier = tiers[mt.group(1)]
            continue
        mh = re.match(r"###\s+\d+\.\s+(.+?)(?:\s+\*\(([^)]*)\)\*)?\s*$", line)
        if mh and tier:
            cur = {"verdict": tier, "paper": mh.group(2) or "", "eq": "", "notes": []}
            out.setdefault(_norm_name(mh.group(1)), []).append(cur)
            continue
        if cur is not None:
            me = re.match(r"-\s+\*\*Equation:\*\*\s*`?(.*?)`?\s*$", line)
            if me:
                cur["eq"] = me.group(1)
                continue
            mn = re.match(r"-\s+\*\*(Math|Empirical)[^:]*:\*\*\s*(.+)$", line)
            if mn:
                cur["notes"].append(f"{mn.group(1)}: {mn.group(2)}")
    return out


# painter per theorem — only where a canvas can draw the ACTUAL relation.
# Cards with no honest drawing get no drawing (house rule: tool, not toy).
_PAINTER_KEYS = [
    ("nullledger", "nullLedger"), ("meancircle", "meanCircle"),
    ("42crossing", "crossing42"), ("crossingsignature", "crossing42"),
    ("foldoperator", "foldCollapse"), ("geometricstatecollapse", "foldCollapse"),
    ("riemann", "riemannLine"), ("observerequivalence", "observerViews"),
    ("observerequation", "observerAngle"), ("observercoordinate", "observerAngle"),
    ("binarydiagonal", "binaryDiagonal"), ("345momentumlock", "lock345"),
    ("massasimpedance", "impedance"), ("massasimaginaryimpedance", "impedance"),
    ("massascomputational", "impedance"), ("pvsnp", "searchCheck"),
    ("primebasedstability", "primeAnchors"), ("timecrystal", "heartbeat121"),
    ("lost2", "lost2"), ("darkmatter", "lost2"),
    ("hierarchyproblem", "scaleRatio"), ("consciousnessresolution", "pixelGrid"),
    ("bifurcationofzero", "zeroSplit"), ("divineequation", "quatSandwich"),
    ("complexhypotenuse", "hypDepth"), ("tadah", "phaseLock"),
    ("pauliexclusion", "meanCircle"), ("morphicresonance", "gcdRing"),
    ("latticeconstruction", "attoDelay"),
]


def _painter_for(nname: str):
    for key, strat in _PAINTER_KEYS:
        if key in nname:
            return strat
    return None


@app.route("/codex")
def codex_page():
    return send_file(CODEX_HTML)


@app.route("/api/codex")
def api_codex():
    if _CODEX_CACHE["data"] and time.time() - _CODEX_CACHE["ts"] < 300:
        return jsonify(_CODEX_CACHE["data"])
    try:
        return _build_codex()
    except Exception as e:
        # A JSON error the page can display beats Flask's HTML 500,
        # which a fetch().json() can only report as a syntax error.
        return jsonify({"error": f"codex build failed: {type(e).__name__}: {e}"}), 500


def _build_codex():
    c = cfg()
    rows = _parse_theorem_table()
    if not rows:
        return jsonify({"error": "theorem index not found",
                        "hint": str(THEOREM_INDEX)}), 404
    audit = _parse_audit(c)
    rvs = read_json(RUNTIME_VS_SPEC) or {}
    covered = {_norm_name(x.get("name", "")): x
               for x in (rvs.get("covered") or []) if isinstance(x, dict)}
    orphans = {_norm_name(x.get("name", ""))
               for x in (rvs.get("orphans") or []) if isinstance(x, dict)}

    cards, tally = [], {"solid": 0, "check": 0, "broken": 0, "variant": 0, "unaudited": 0}
    for r in rows:
        nn = _norm_name(r["name"])
        card = dict(r)
        card["axiom"] = ("axiom" in r["derivation"].lower()
                         or "foundational" in r["derivation"].lower())
        cov = covered.get(nn)
        card["dreams"] = int(cov["dreams"]) if cov else 0
        card["peak_urgency"] = cov.get("peak_urgency") if cov else None
        card["orphan"] = nn in orphans or (not cov and bool(orphans))
        # --- audit verdict, equation-aware ---
        entries = audit.get(nn) or []
        verdict, vnotes, veq = None, [], ""
        if entries:
            my_eq = _norm_eq(r["equation"])
            usable = [(a, _norm_eq(a["eq"])) for a in entries]
            exact = [a for a, e in usable if len(e) >= 4 and e == my_eq]
            # AUDIT.md's own generator sometimes truncates equations at a pipe
            # (Ĥ|Ψ> became "Ĥ\") — a near-empty audit equation means "none
            # recorded", so the verdict transfers on the name alone.
            noeq = [a for a, e in usable if len(e) < 4]
            if exact:
                pick = exact[0]
                verdict, vnotes, veq = pick["verdict"], pick["notes"], pick["eq"]
            elif noeq:
                pick = noeq[0]
                verdict, vnotes = pick["verdict"], pick["notes"]
            else:
                # name matches, equation does not: the audit judged a different
                # printed form. Never transfer that verdict — flag it instead.
                pick = entries[0]
                verdict = "variant"
                veq = pick["eq"]
                vnotes = [f"audit examined a different printed form ({pick['verdict']})"] + pick["notes"]
        card["verdict"] = verdict or "unaudited"
        card["verdict_notes"] = vnotes[:3]
        card["audit_eq"] = veq if verdict == "variant" else ""
        tally[card["verdict"]] = tally.get(card["verdict"], 0) + 1
        card["painter"] = _painter_for(nn)
        cards.append(card)

    payload = {
        "theorems": cards,
        "counts": {"total": len(cards),
                   "dreamt": sum(1 for x in cards if x["dreams"] > 0),
                   "orphans": sum(1 for x in cards if x["orphan"]),
                   "animated": sum(1 for x in cards if x["painter"]),
                   "verdicts": tally},
        "emerging": (rvs.get("emerging") or [])[:20],
        "runtime_generated": rvs.get("generated"),
        "runtime_dreams": rvs.get("dreams"),
        "audit_found": bool(audit),
    }
    _CODEX_CACHE["data"], _CODEX_CACHE["ts"] = payload, time.time()
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
    # ?force=1 — the FORCE PREVIEW button: compute the full forecast now even
    # below the charge gate. It is labelled preview, never persisted and never
    # cached: a reading taken on demand is a what-if, not a measurement, and
    # must not enter the scoreable record or masquerade as the scheduled poll.
    force = request.args.get("force") == "1"
    if not force and _FORECAST_CACHE["data"] and time.time() - _FORECAST_CACHE["ts"] < 900:
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
        if not force and out["charged"] and not out.get("degraded") and regions:
            try:
                if not fc.has_open_forecast():
                    rec = fc.build_forecast(st, regions, _total,
                                            forced=False, issued_by="deck")
                    if rec:
                        fc.write_forecast(rec)
                        out["issued_id"] = rec["id"]
            except Exception:
                pass

        if force:
            out["preview"] = True
        else:
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


# --- controls: buttons that DO something -----------------------------------
# The deck was all gauges. These are the first controls, and each one drives a
# real mechanism — no theatre. Flush writes dirty FAISS indices to disk NOW
# (what stop_grid does before killing); dream_now cuts the inter-dream wait
# short via the engine's wake event; aether_refresh invalidates the 5-min
# cache so the next poll hits the live feeds.
@app.route("/api/control/flush", methods=["POST"])
def api_control_flush():
    try:
        r = http.post(f"{bridge(cfg())}/flush", timeout=60)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"status": "error", "message": f"engine unreachable: {e}"}), 503


@app.route("/api/control/dream_now", methods=["POST"])
def api_control_dream_now():
    try:
        r = http.post(f"{bridge(cfg())}/dream_now", timeout=10)
        if r.status_code == 404:
            return jsonify({"status": "error",
                            "message": "engine predates /dream_now — restart the grid"}), 501
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({"status": "error", "message": f"engine unreachable: {e}"}), 503


@app.route("/api/control/aether_refresh", methods=["POST"])
def api_control_aether_refresh():
    AETHER_CACHE["ts"] = 0.0
    return jsonify({"status": "success", "message": "aether cache invalidated — next poll is live"})


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
SKILLS_DIR = ROOT / "skills"      # the Circle's own playbook — workflows it saves for itself
SANDBOX_DIR = ROOT / "sandbox"    # run_python's cwd; files persist across calls, stays local
SECRET_NAMES = {"config.json", ".env", "credentials.json", "token.json", "secrets.json"}
READABLE_SUFFIXES = {".md", ".txt", ".json", ".py", ".html", ".css", ".js", ".jsonl", ".csv", ".yml", ".yaml"}


# --- web: SSRF gate (ported from LumOS web_tools, requests-flavoured) -------
# The gate is load-bearing HERE, not theatre: the grid's own services listen on
# 127.0.0.1 (:5000 engine, :7777 deck, :1234 LM Studio). A fetched page that
# told the model to "check http://127.0.0.1:5000/flush" must hit a wall. The
# authoritative check resolves the hostname and refuses if ANY address is
# non-public — that also catches decimal/hex IP literals and public names that
# resolve inward. Re-validated on every redirect hop.
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
_STYLE_RE = re.compile(r"<style\b[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE)
_ENTITIES = {"&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
             "&#39;": "'", "&apos;": "'", "&mdash;": "—", "&ndash;": "–", "&hellip;": "…"}


def _strip_html(html: str) -> str:
    text = _SCRIPT_RE.sub(" ", html)
    text = _STYLE_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    for k, v in _ENTITIES.items():
        text = text.replace(k, v)
    return re.sub(r"\s+", " ", text).strip()


def _ip_blocked(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return (ip.is_loopback or ip.is_private or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _url_blocked(url: str):
    """Return a refusal reason, or None if the URL is safe to fetch."""
    try:
        parsed = urlparse(url)
    except Exception:
        return "malformed URL"
    if parsed.scheme not in ("http", "https"):
        return f"scheme '{parsed.scheme}' not allowed"
    host = parsed.hostname
    if not host:
        return "no host"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        return f"DNS resolution failed: {e}"
    if not infos:
        return "no addresses resolved"
    for info in infos:
        if _ip_blocked(info[4][0]):
            return "resolves to a non-public address (the grid itself lives there) — refused"
    return None


# --- run_python: AST guard (ported whole from LumOS python_tools) ------------
# Hardened after CONFIRMED bypasses of a naive blocklist (`import os as o;
# o.system(...)` and `os.__dict__["system"]`). Three layers: imports blocked at
# the statement, dangerous attribute NAMES blocked receiver-agnostically, and
# introspection dunders blocked outright. Do not trim these sets.
_PY_BLOCKED_MODULES = frozenset({
    "os", "sys", "importlib", "builtins", "gc", "code", "codeop", "runpy",
    "pdb", "bdb", "inspect", "signal", "platform", "subprocess", "socket",
    "ssl", "ftplib", "telnetlib", "smtplib", "poplib", "imaplib", "http",
    "urllib", "urllib3", "httpx", "requests", "asyncio", "threading",
    "multiprocessing", "ctypes", "cffi", "pty", "fcntl", "termios", "winreg",
    "msvcrt", "_winapi", "shutil", "tempfile", "glob", "pickle", "shelve",
    "marshal",
})
_PY_BLOCKED_NAMES = frozenset({
    "eval", "exec", "compile", "open", "__import__", "globals", "locals",
    "vars", "getattr", "setattr", "delattr",
})
_PY_BLOCKED_ATTRS = frozenset({
    "system", "popen", "posix_spawn", "posix_spawnp", "exec", "execl",
    "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe",
    "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve",
    "spawnvp", "spawnvpe", "fork", "forkpty", "setuid", "setgid",
    "setreuid", "setregid",
})
_PY_BLOCKED_DUNDERS = frozenset({
    "__class__", "__bases__", "__base__", "__mro__", "__subclasses__",
    "__globals__", "__builtins__", "__dict__", "__getattribute__",
    "__import__", "__loader__", "__spec__",
})
_PY_BLOCKED_OS_ATTRS = frozenset({
    "remove", "unlink", "removedirs", "rmdir", "chmod", "chown", "rename",
    "replace", "symlink", "link", "kill", "killpg",
})


def _scan_python(code: str):
    """AST-walk the snippet. Returns an error string, or None if clean."""
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as e:
        return f"syntax error: {e}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _PY_BLOCKED_MODULES:
                    return f"blocked import: {alias.name}"
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in _PY_BLOCKED_MODULES:
                return f"blocked import: from {node.module}"
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in _PY_BLOCKED_NAMES:
                return f"blocked call: {fn.id}()"
            if (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                    and fn.value.id == "os" and fn.attr in _PY_BLOCKED_OS_ATTRS):
                return f"blocked call: os.{fn.attr}()"
        if isinstance(node, ast.Attribute):
            if node.attr in _PY_BLOCKED_ATTRS:
                return f"blocked attribute: .{node.attr} (process/exec escape)"
            if node.attr in _PY_BLOCKED_DUNDERS:
                return f"blocked attribute: {node.attr} (introspection escape)"
    return None


def _scrubbed_env() -> dict:
    """Sandbox env: inherit the OS env minus anything that smells like a secret."""
    bad = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PWD", "CREDENTIAL", "AUTH")
    return {k: v for k, v in os.environ.items()
            if not any(b in k.upper() for b in bad)}


# --- find_contradictions: stance markers (ported from LumOS memory_tools) ---
# Not a stance classifier — recall over precision. A marker within ~80 chars of
# a claim keyword makes the chunk a CANDIDATE; the persona reads and decides.
_NEG_MARKERS = ("not", "no ", "never", "isn't", "wasn't", "aren't", "doesn't",
                "didn't", "won't", "can't", "cannot", "however", "but ",
                "actually", "wrong", "incorrect", "false", "disagree",
                "instead", "rather", "contrary", "contradict", "refute",
                "mistaken", "revised", "retracted")
_POS_MARKERS = ("agree", "confirm", "exactly", "indeed", "correct", "verified",
                "consistent", "matches", "aligned", "supports")
_STOPWORDS = frozenset(
    "the a an is are was were be been being of to in on at for with by from "
    "as and or but if then this that these those it its i we you he she they "
    "them us my your his her their our do does did".split())


def _claim_keywords(claim: str, max_terms: int = 8) -> list:
    seen, out = set(), []
    for w in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", claim.lower()):
        if w in _STOPWORDS or w in seen:
            continue
        seen.add(w); out.append(w)
        if len(out) >= max_terms:
            break
    return out


def _classify_stance(terms: list, chunk: str):
    lower = chunk.lower()
    kpos = [i for t in terms for i in _find_all(lower, t)]
    if not kpos:
        return "unclear", []
    def hits(markers):
        got = []
        for m in markers:
            if any(abs(i - k) <= 80 for i in _find_all(lower, m) for k in kpos):
                got.append(m.strip())
        return got
    neg, pos = hits(_NEG_MARKERS), hits(_POS_MARKERS)
    if len(neg) > len(pos):
        return "contradicts", neg
    if len(pos) > len(neg):
        return "supports", pos
    return "unclear", []


def _find_all(hay: str, needle: str) -> list:
    out, start = [], 0
    while True:
        i = hay.find(needle, start)
        if i < 0:
            return out
        out.append(i); start = i + len(needle)


# --- repo introspection: read-only git, hardwired to this repo --------------
# The Circle may look at its own history; committing and pushing stay at the
# operator's desk. No repo_path argument on purpose.
def _run_git(args: list) -> str:
    try:
        r = subprocess.run(["git"] + args, cwd=str(ROOT), capture_output=True,
                           text=True, timeout=15, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return "error: git not on PATH"
    except subprocess.TimeoutExpired:
        return "error: git timed out (15s)"
    out = (r.stdout or "").strip() or (r.stderr or "").strip() or "(no output)"
    return out[:5000] + ("\n… (truncated)" if len(out) > 5000 else "")


# --- temporal scan: UBBM binary-diagonal θ + autocorrelation -----------------
# θ = arctan(ones/zeros) over the utf-8 BITS of a dream's text — the published
# binaryDiagonalBearing helper, applied per ping. Autocorrelation of the θ
# series answers "is the engine cycling on themes, or drifting?" with the
# framework's own arithmetic instead of vibes.
def _binary_theta(text: str) -> float:
    ones = zeros = 0
    for b in text.encode("utf-8", errors="replace"):
        n = bin(b).count("1")
        ones += n; zeros += 8 - n
    return math.atan2(ones, zeros) if (ones or zeros) else 0.0


def _autocorr_peaks(series: list, max_lag: int) -> list:
    n = len(series)
    if n < 4:
        return []
    mean = sum(series) / n
    c = [v - mean for v in series]
    denom = sum(v * v for v in c) or 1.0
    peaks = [{"lag": lag, "rho": round(sum(c[i] * c[i + lag] for i in range(n - lag)) / denom, 4)}
             for lag in range(1, min(max_lag, n - 1) + 1)]
    peaks.sort(key=lambda p: -p["rho"])
    return peaks[:3]


def _recent_pings(n: int) -> list:
    """Newest n dream pings (relay + archive), oldest→newest for time series."""
    cands = []
    for folder in (RELAY, RELAY / "processed_pings"):
        if folder.exists():
            for f in folder.glob("ping_*.json*"):
                try:
                    cands.append((f.stat().st_mtime, f))
                except OSError:
                    continue
    cands.sort(key=lambda x: -x[0])
    out = []
    for mtime, f in cands[:n]:
        d = read_json(f)
        if isinstance(d, dict):
            out.append((mtime, d))
    out.reverse()
    return out

TOOL_SPECS = [
    # --- memory -----------------------------------------------------------
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
        "name": "find_contradictions",
        "description": "Before asserting something strongly, sweep memory for chunks that may "
                       "SAY THE OPPOSITE. Returns candidates grouped contradicts/supports/unclear "
                       "— read them and judge; the grouping is lexical, not gospel.",
        "parameters": {"type": "object", "properties": {
            "claim": {"type": "string", "description": "The assertion to check, e.g. 'the mass gap floor is 0.657'."},
            "top_k": {"type": "integer", "description": "Candidate chunks to scan (3-20, default 10)."}},
            "required": ["claim"]}}},
    {"type": "function", "function": {
        "name": "cite_source",
        "description": "Look a memory chunk up by its address (lane, idx — shown in search_memory "
                       "results) and return a citation with provenance: lane, cluster, metadata, snippet. "
                       "Use when a claim from retrieval needs to be checkable in a paper or report.",
        "parameters": {"type": "object", "properties": {
            "lane": {"type": "string", "description": "conversations | knowledge | shared."},
            "idx": {"type": "integer", "description": "The chunk index from the search result tag."}},
            "required": ["lane", "idx"]}}},
    {"type": "function", "function": {
        "name": "grid_status",
        "description": "The Grid's own vitals right now: engine health, per-lane chunk/vector "
                       "counts, dream cadence, Soul Engine coherence, Echo queue, space weather. "
                       "Use when asked how the Grid is running or how you are feeling.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    # --- files ------------------------------------------------------------
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
        "name": "append_note",
        "description": "Append to an existing note in notes/ — for journals and logs that grow. "
                       "Creates the note if missing. Use list_notes to find filenames.",
        "parameters": {"type": "object", "properties": {
            "filename": {"type": "string", "description": "Note filename, e.g. '20260820-observations.md'."},
            "text": {"type": "string", "description": "Markdown to append."}},
            "required": ["filename", "text"]}}},
    {"type": "function", "function": {
        "name": "list_notes",
        "description": "List the notes the Circle has saved (newest first, with sizes).",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    # --- web --------------------------------------------------------------
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the public web (Tavily). Use for recent events, published papers, "
                       "current data — anything not in the Grid's memory. Returns titles, URLs, "
                       "snippets; pair with fetch_url to read a result in full.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Free-text search query."},
            "max_results": {"type": "integer", "description": "1-10, default 5."}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "fetch_url",
        "description": "Fetch a public web page as plain text (HTML stripped). Use after web_search, "
                       "or when given a URL. Localhost and private addresses are refused — the "
                       "Grid's own services are not reachable this way.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "http(s) URL."}},
            "required": ["url"]}}},
    # --- compute ----------------------------------------------------------
    {"type": "function", "function": {
        "name": "run_python",
        "description": "Execute a Python snippet in a sandboxed subprocess — for maths, checking "
                       "an equation actually evaluates, CSV work, regex tests. print() the result; "
                       "stdout is returned. 30s limit, cwd is the local sandbox/ folder (files "
                       "persist between calls). os/sys/subprocess/network and open() are blocked — "
                       "use pathlib for sandbox files. Available: math, statistics, json, re, "
                       "datetime, decimal, fractions, itertools, collections, csv, pathlib, "
                       "plus numpy/sympy/matplotlib if installed.",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string", "description": "Python source. Use print() for results."}},
            "required": ["code"]}}},
    # --- skills -----------------------------------------------------------
    {"type": "function", "function": {
        "name": "list_skills",
        "description": "List the Circle's saved skills — workflow playbooks written by the Circle "
                       "itself. Check here before improvising a recurring kind of task.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "read_skill",
        "description": "Read one skill by name to follow its workflow.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Skill name from list_skills."}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "save_skill",
        "description": "Save a skill: when a workflow proved effective and will recur, write it "
                       "down for next time. Sections: # Title, ## When to use, ## Approach, "
                       "## Tools to chain. Overwrites same-named skill.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Alphanumeric/_/- name."},
            "content": {"type": "string", "description": "Markdown body."}},
            "required": ["name", "content"]}}},
    # --- repo (read-only) ---------------------------------------------------
    {"type": "function", "function": {
        "name": "git_status",
        "description": "Working-tree status of the Awen Engine repo (read-only).",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "git_log",
        "description": "Recent commits of the Awen Engine repo (read-only).",
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "How many commits (1-30, default 10)."}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "git_diff",
        "description": "Current uncommitted changes in the Awen Engine repo (read-only). "
                       "Default is a per-file summary; full=true for the actual diff text.",
        "parameters": {"type": "object", "properties": {
            "staged": {"type": "boolean", "description": "true = staged changes only."},
            "full": {"type": "boolean", "description": "true = full diff, not just --stat."}},
            "required": []}}},
    # --- time -------------------------------------------------------------
    {"type": "function", "function": {
        "name": "current_time",
        "description": "The current local date and time.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "temporal_pattern_scan",
        "description": "Scan the recent dream archive for cyclic patterns: UBBM binary-diagonal θ "
                       "per ping + autocorrelation of the series. Answers 'is the engine cycling "
                       "on themes or drifting?' with the framework's own arithmetic.",
        "parameters": {"type": "object", "properties": {
            "n_dreams": {"type": "integer", "description": "How many recent pings (8-200, default 60)."}},
            "required": []}}},
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
                # lane:idx is a citable address — cite_source() resolves it.
                addr = f"{h.get('source','?')}"
                if h.get("idx") is not None:
                    addr += f":{h['idx']}"
                tag = f" {h['cluster']}" if h.get("cluster") else ""
                out.append(f"[{i}] ({addr}, d={h.get('distance',0):.3f}{tag}) {txt}")
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
            # Path.glob honours '..' and absolute patterns, so validating the
            # directory alone is not enough — '../x/*' or 'C:/x/*' would walk
            # out of a checked root. Reject those shapes, then defensively drop
            # any hit that RESOLVES outside ROOT (catches symlinks/junctions).
            if ".." in Path(pat).parts or pat.startswith(("/", "\\")) or (len(pat) > 1 and pat[1] == ":"):
                return "error: pattern may not contain '..', an absolute path, or a drive letter"
            root_resolved = str(ROOT.resolve())
            found = sorted(p for p in ROOT.glob(pat)
                           if p.is_file() and p.name.lower() not in SECRET_NAMES
                           and str(p.resolve()).startswith(root_resolved))
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

        if name == "web_search":
            q = str(args.get("query", "")).strip()
            if not q:
                return "error: query is required"
            n = max(1, min(10, int(args.get("max_results", 5) or 5)))
            key = str(c.get("tavily_api_key", "")).strip()
            if key:
                try:
                    r = http.post("https://api.tavily.com/search",
                                  json={"api_key": key, "query": q, "max_results": n,
                                        "include_answer": True, "include_raw_content": False},
                                  timeout=20)
                    r.raise_for_status()
                    d = r.json()
                    lines = []
                    if d.get("answer"):
                        lines.append(f"summary: {str(d['answer']).strip()[:600]}")
                    for i, x in enumerate(d.get("results") or [], 1):
                        lines.append(f"[{i}] {str(x.get('title',''))[:100]} — {x.get('url','')}")
                        snip = " ".join(str(x.get("content", "")).split())[:400]
                        if snip:
                            lines.append(f"    {snip}")
                    return "\n".join(lines) if lines else "no results"
                except Exception as e:
                    pass  # fall through to DDG — a search key dying must not blind the Circle
            try:
                from ddgs import DDGS
            except ImportError:
                return ("error: no web search backend (tavily failed or unconfigured, "
                        "ddgs not installed — `py -3.11 -m pip install ddgs`)")
            try:
                lines = []
                with DDGS() as dd:
                    for i, hit in enumerate(dd.text(q, max_results=n), 1):
                        lines.append(f"[{i}] {str(hit.get('title',''))[:100]} — {hit.get('href') or hit.get('url','')}")
                        snip = " ".join(str(hit.get("body") or "").split())[:400]
                        if snip:
                            lines.append(f"    {snip}")
                return "\n".join(lines) if lines else "no results"
            except Exception as e:
                return f"error: web search failed: {e}"

        if name == "fetch_url":
            url = str(args.get("url", "")).strip()
            if not url.startswith(("http://", "https://")):
                return "error: URL must start with http:// or https://"
            current = url
            # follow redirects BY HAND so every hop passes the SSRF gate —
            # auto-follow would happily chase a 302 to 127.0.0.1:5000.
            for _hop in range(6):
                reason = _url_blocked(current)
                if reason:
                    return f"error: blocked URL ({reason})"
                try:
                    r = http.get(current, timeout=20, allow_redirects=False,
                                 headers={"User-Agent": "AwenGrid/2.1 (local research deck)"})
                except requests.Timeout:
                    return "error: request timed out (20s)"
                except Exception as e:
                    return f"error: {e}"
                loc = r.headers.get("location")
                if r.status_code in (301, 302, 303, 307, 308) and loc:
                    nxt = urljoin(current, loc)
                    if not nxt.startswith(("http://", "https://")):
                        return "error: redirect to non-http(s) scheme blocked"
                    current = nxt
                    continue
                if r.status_code >= 400:
                    return f"error: HTTP {r.status_code} at {current}"
                ctype = r.headers.get("content-type", "").lower()
                text = _strip_html(r.text) if "html" in ctype else r.text
                if len(text) > 7000:
                    return f"{current}\n{text[:7000]}\n… (truncated at 7000 chars)"
                return f"{current}\n{text}"
            return "error: too many redirects (>5)"

        if name == "run_python":
            code = str(args.get("code", ""))
            if not code.strip():
                return "error: empty code"
            blocked = _scan_python(code)
            if blocked:
                return f"error: sandbox refused — {blocked}"
            SANDBOX_DIR.mkdir(exist_ok=True)
            start_ts = time.time()
            try:
                r = subprocess.run([sys.executable, "-I", "-c", code],
                                   cwd=str(SANDBOX_DIR), capture_output=True, text=True,
                                   timeout=30, encoding="utf-8", errors="replace",
                                   env=_scrubbed_env())
            except subprocess.TimeoutExpired:
                return "error: timed out after 30s (infinite loop?)"
            except Exception as e:
                return f"error: subprocess failed: {e}"
            out = []
            if r.stdout:
                s = r.stdout.strip()
                out.append(s[-3000:] if len(s) > 3000 else s)
            if r.returncode != 0 and r.stderr:
                s = r.stderr.strip()
                out.append("stderr: " + (s[-1500:] if len(s) > 1500 else s))
            made = [p.name for p in SANDBOX_DIR.glob("*")
                    if p.is_file() and p.stat().st_mtime > start_ts - 1]
            if made:
                out.append(f"files written to sandbox/: {', '.join(sorted(made)[:10])}")
            out.append(f"exit code {r.returncode}")
            return "\n".join(out)

        if name == "find_contradictions":
            claim = str(args.get("claim", "")).strip()
            if not claim:
                return "error: claim is required"
            k = max(3, min(20, int(args.get("top_k", 10) or 10)))
            r = requests.post(f"{bridge(c)}/search",
                              json={"query": claim, "node": node, "params": {"top_k": k}},
                              timeout=90)
            r.raise_for_status()
            hits = r.json()
            if not isinstance(hits, list) or not hits:
                return "no related chunks found — the claim has no echo in memory either way"
            terms = _claim_keywords(claim)
            groups = {"contradicts": [], "supports": [], "unclear": []}
            for h in hits:
                txt = str(h.get("chunk", ""))
                stance, markers = _classify_stance(terms, txt)
                addr = f"{h.get('source','?')}" + (f":{h['idx']}" if h.get("idx") is not None else "")
                line = f"({addr}) {' '.join(txt.split())[:300]}"
                if markers:
                    line += f"  [markers: {', '.join(markers[:4])}]"
                groups[stance].append(line)
            out = [f"claim: {claim}", f"keywords: {', '.join(terms)}"]
            for g in ("contradicts", "supports", "unclear"):
                out.append(f"\n{g.upper()} ({len(groups[g])}):")
                out.extend(groups[g][:5] or ["  (none)"])
            out.append("\nNOTE: lexical grouping, recall over precision — read before you trust it.")
            return "\n".join(out)

        if name == "cite_source":
            lane = str(args.get("lane", "")).strip()
            try:
                idx = int(args.get("idx", -1))
            except (TypeError, ValueError):
                return "error: idx must be an integer"
            r = requests.get(f"{bridge(c)}/chunk",
                             params={"profile": lane, "idx": idx, "node": node}, timeout=30)
            if r.status_code == 404 and "chunk" not in r.text:
                return ("error: the engine predates the /chunk endpoint — "
                        "restart the grid to enable citations")
            d = r.json()
            if d.get("error"):
                return f"error: {d['error']}"
            lines = [f"citation: {lane}[{idx}]"]
            if d.get("cluster"):
                lines.append(f"cluster: {d['cluster']}")
            meta = d.get("meta") or {}
            for mk in ("title", "source", "node", "ts", "date"):
                if meta.get(mk):
                    lines.append(f"{mk}: {str(meta[mk])[:120]}")
            lines.append("text: " + " ".join(str(d.get("chunk", "")).split())[:500])
            return "\n".join(lines)

        if name == "grid_status":
            lines = []
            try:
                h = http.get(f"{bridge(c)}/health", timeout=3).json()
                lines.append(f"engine: {h.get('status', '?')}")
            except Exception:
                lines.append("engine: OFFLINE")
            try:
                s = http.get(f"{bridge(c)}/stats", timeout=5).json()
                for lane_name, p in (s.get("profiles") or {}).items():
                    lines.append(f"  {lane_name}: {p.get('chunks',0):,} chunks · "
                                 f"{p.get('vectors',0):,} vectors · "
                                 f"{p.get('dream_insights',0):,} insights · "
                                 f"{p.get('dirty_unflushed',0)} unflushed")
                scores = s.get("recent_dream_scores") or []
                if scores:
                    lines.append(f"  recent dream urgencies: {scores[-6:]}")
            except Exception:
                lines.append("  stats unavailable")
            soul = read_json(ROOT / "grid_heartbeat.json") or {}
            if soul:
                lines.append(f"soul engine: coherence {soul.get('coherence','?')} · "
                             f"torsion {soul.get('torsion_index','?')} · "
                             f"harmonic {soul.get('current_harmonic','?')} · "
                             f"mode {soul.get('mode','?')}")
            echo = read_json(ROOT / "echo_heartbeat.json") or {}
            if echo:
                lines.append(f"echo: queue {echo.get('queue_pending','?')} · "
                             f"last beat {echo.get('timestamp','?')} · "
                             f"{'enabled' if echo.get('enabled') else 'disabled'}")
            aeth = AETHER_CACHE.get("data") or {}
            if aeth and time.time() - AETHER_CACHE.get("ts", 0) < 900:
                bits = []
                for kk, lbl in (("kp", "Kp"), ("sw_speed", "wind km/s"), ("xray_class", "x-ray")):
                    if aeth.get(kk) is not None:
                        bits.append(f"{lbl} {aeth[kk]}")
                if bits:
                    lines.append("aether: " + " · ".join(bits))
            pings = _recent_pings(1)
            if pings:
                age_min = (time.time() - pings[-1][0]) / 60
                lines.append(f"newest dream: {age_min:.0f} min ago "
                             f"({pings[-1][1].get('agent_name','?')}, urgency {pings[-1][1].get('urgency','?')})")
            return "\n".join(lines) or "no grid state readable"

        if name == "append_note":
            fname = Path(str(args.get("filename", ""))).name  # basename only — no paths
            if not fname.endswith(".md"):
                fname += ".md"
            if not re.match(r"^[\w\-. ]+\.md$", fname):
                return "error: filename must be a plain .md name"
            NOTES_DIR.mkdir(exist_ok=True)
            p = NOTES_DIR / fname
            addition = f"\n\n---\n_{datetime.now().strftime('%Y-%m-%d %H:%M')}_\n\n{str(args.get('text',''))}\n"
            existing = p.stat().st_size if p.exists() else 0
            if existing + len(addition.encode("utf-8")) > 2_000_000:
                return "error: note would exceed 2MB — start a new one with write_note"
            with p.open("a", encoding="utf-8") as f:
                f.write(addition)
            return f"appended to notes/{fname} (now {p.stat().st_size:,} bytes)"

        if name == "list_notes":
            if not NOTES_DIR.exists():
                return "no notes yet"
            notes = sorted(NOTES_DIR.glob("*.md"), key=lambda p: -p.stat().st_mtime)
            if not notes:
                return "no notes yet"
            return "\n".join(f"{p.name}  ({p.stat().st_size/1024:.1f} KB, "
                             f"{datetime.fromtimestamp(p.stat().st_mtime).strftime('%Y-%m-%d %H:%M')})"
                             for p in notes[:60])

        if name == "list_skills":
            SKILLS_DIR.mkdir(exist_ok=True)
            skills = sorted(SKILLS_DIR.glob("*.md"))
            if not skills:
                return "no skills saved yet — save_skill writes the first one"
            out = []
            for p in skills:
                first = ""
                try:
                    for ln in p.read_text(encoding="utf-8").splitlines():
                        if ln.strip():
                            first = ln.strip().lstrip("# ")[:110]
                            break
                except OSError:
                    pass
                out.append(f"{p.stem} — {first}")
            return "\n".join(out)

        if name == "read_skill":
            safe = re.sub(r"[^\w\-]", "", str(args.get("name", "")))
            if not safe:
                return "error: skill name must be alphanumeric/_/-"
            p = SKILLS_DIR / f"{safe}.md"
            if not p.exists():
                return f"error: skill '{safe}' not found — use list_skills"
            return p.read_text(encoding="utf-8")[:8000]

        if name == "save_skill":
            safe = re.sub(r"[^\w\-]", "", str(args.get("name", "")))
            if not safe:
                return "error: skill name must be alphanumeric/_/-"
            content = str(args.get("content", ""))
            if len(content.encode("utf-8")) > 100_000:
                return "error: skill too large (max 100KB)"
            SKILLS_DIR.mkdir(exist_ok=True)
            p = SKILLS_DIR / f"{safe}.md"
            existed = p.exists()
            p.write_text(content, encoding="utf-8")
            return f"skill '{safe}' {'updated' if existed else 'saved'}"

        if name == "git_status":
            return _run_git(["status", "--short", "--branch"])

        if name == "git_log":
            n = max(1, min(30, int(args.get("limit", 10) or 10)))
            return _run_git(["log", f"--max-count={n}", "--oneline", "--decorate"])

        if name == "git_diff":
            a = ["diff", "--stat"] if not args.get("full") else ["diff"]
            if args.get("staged"):
                a.append("--cached")
            return _run_git(a)

        if name == "temporal_pattern_scan":
            n = max(8, min(200, int(args.get("n_dreams", 60) or 60)))
            pings = _recent_pings(n)
            if len(pings) < 8:
                return f"error: only {len(pings)} pings on disk — need at least 8"
            thetas = [_binary_theta(str(d.get("seed_text", "")) + str(d.get("synthesis", "")))
                      for _, d in pings]
            peaks = _autocorr_peaks(thetas, max_lag=len(thetas) // 2)
            mean = sum(thetas) / len(thetas)
            var = sum((v - mean) ** 2 for v in thetas) / len(thetas)
            lines = [f"pings analysed: {len(thetas)} (oldest→newest)",
                     f"θ trajectory: first {thetas[0]:.4f} → last {thetas[-1]:.4f} rad · "
                     f"mean {mean:.4f} · σ {math.sqrt(var):.4f} · "
                     f"spread {max(thetas)-min(thetas):.4f}"]
            if peaks:
                lines.append("autocorrelation peaks: " +
                             ", ".join(f"lag {p['lag']} (ρ={p['rho']})" for p in peaks))
                top = peaks[0]
                if top["rho"] >= 0.5:
                    lines.append(f"verdict: strong cycle — the engine returns to a theme every ~{top['lag']} dreams")
                elif top["rho"] >= 0.25:
                    lines.append(f"verdict: moderate periodicity at lag {top['lag']} — some thematic return")
                elif top["rho"] >= 0.1:
                    lines.append(f"verdict: weak periodicity — mostly novel dreams, occasional callbacks")
                else:
                    lines.append("verdict: no significant cycle — the dreaming is drifting/linear")
            return "\n".join(lines)

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


class _ThinkFilter:
    """Strips <think>…</think> from a token stream without buffering the lot.
    Reasoning models narrate their scratchpad first; streamed raw it would
    paint the whole chain-of-thought into the reply bubble and then vanish.
    Holds back only enough tail to catch a tag split across two chunks."""
    def __init__(self):
        self.in_think = False
        self.buf = ""

    def feed(self, s: str) -> str:
        self.buf += s
        out = []
        while True:
            if self.in_think:
                j = self.buf.find("</think>")
                if j < 0:
                    self.buf = self.buf[-8:]      # keep a possible tag prefix
                    break
                self.buf = self.buf[j + 8:]
                self.in_think = False
            else:
                j = self.buf.find("<think>")
                if j >= 0:
                    out.append(self.buf[:j])
                    self.buf = self.buf[j + 7:]
                    self.in_think = True
                    continue
                keep = 0
                for k in range(min(6, len(self.buf)), 0, -1):
                    if self.buf.endswith("<think>"[:k]):
                        keep = k
                        break
                out.append(self.buf[:-keep] if keep else self.buf)
                self.buf = self.buf[-keep:] if keep else ""
                break
        return "".join(out)

    def flush(self) -> str:
        s = "" if self.in_think else self.buf
        self.buf = ""
        return s


def llm_stream(c: dict, cloud: bool, messages: list, tools=None):
    """Streamed completion. Yields ("delta", text) as tokens arrive, then one
    ("final", {content, tool_calls, usage}). Tool-call fragments are reassembled
    by index per the OpenAI streaming contract. stream_options is retried
    without on a 400 — older LM Studio builds reject it."""
    if cloud:
        nv = nvidia_block(c)
        url = str(nv.get("base_url", "https://integrate.api.nvidia.com/v1")).rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {str(nv.get('api_key','')).strip()}"}
        payload = {"model": str(nv.get("model", "")).strip(), "messages": messages,
                   "temperature": float(c.get("lmstudio_temp", 0.6)),
                   "max_tokens": int(nv.get("max_tokens", 4096))}
        timeout = int(nv.get("timeout", 300))
    else:
        url = str(c.get("lmstudio_url", "http://localhost:1234")).rstrip("/") + "/v1/chat/completions"
        headers = {}
        payload = {"model": str(c.get("light_model") or c.get("deep_model") or "").strip(),
                   "messages": messages, "temperature": float(c.get("lmstudio_temp", 0.6))}
        mt = int(c.get("lmstudio_max_tokens", -1))
        if mt > 0:
            payload["max_tokens"] = mt
        timeout = int(c.get("lmstudio_timeout", 800))
    if tools:
        payload["tools"] = tools
    payload["stream"] = True
    payload["stream_options"] = {"include_usage": True}

    r = http.post(url, headers=headers, json=payload, stream=True, timeout=(10, timeout))
    if r.status_code == 400:
        payload.pop("stream_options", None)
        r = http.post(url, headers=headers, json=payload, stream=True, timeout=(10, timeout))
    r.raise_for_status()

    filt = _ThinkFilter()
    content_parts, calls, usage = [], {}, None
    for raw in r.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data:"):
            continue
        data = raw[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except Exception:
            continue
        if chunk.get("usage"):
            usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        if delta.get("content"):
            content_parts.append(delta["content"])
            visible = filt.feed(delta["content"])
            if visible:
                yield ("delta", visible)
        for tc in delta.get("tool_calls") or []:
            i = int(tc.get("index", 0))
            slot = calls.setdefault(i, {"id": "", "type": "function",
                                        "function": {"name": "", "arguments": ""}})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["function"]["name"] += fn["name"]
            if fn.get("arguments"):
                slot["function"]["arguments"] += fn["arguments"]
    tail = filt.flush()
    if tail:
        yield ("delta", tail)
    full = re.sub(r"<think>.*?</think>", "", "".join(content_parts), flags=re.DOTALL).strip()
    yield ("final", {"content": full,
                     "tool_calls": [calls[i] for i in sorted(calls)] or None,
                     "usage": usage})


@app.route("/api/tools")
def api_tools():
    """What the Circle can reach for. Handy for the deck to display."""
    c = cfg()
    enabled = bool((c.get("client_config") or {}).get("tools_enabled", True))
    return jsonify({"enabled": enabled,
                    "tools": [{"name": t["function"]["name"],
                               "description": t["function"]["description"]}
                              for t in TOOL_SPECS]})


def _chat_context(body: dict):
    """Shared prep for /api/chat and /api/chat_stream \u2014 ONE implementation of
    persona resolution, memory retrieval and the tools preamble, so the two
    routes can never drift apart (the seismic lesson, applied to plumbing).
    Returns (ctx, None) or (None, flask error response)."""
    message = str(body.get("message", "")).strip()
    state_name = str(body.get("state", "")).strip()
    node = str(body.get("node", "lumos")).strip()
    use_memory = bool(body.get("use_memory", True))
    do_index = bool(body.get("index", True))
    if not message:
        return None, (jsonify({"error": "empty message"}), 400)

    c = cfg()
    states = c.get("cognitive_states") or {}
    # Never silently substitute a persona: answering as Lumos when Veritas was
    # asked for would be indexed into memory under the wrong attribution.
    if state_name and state_name not in states:
        return None, (jsonify({"error": f"unknown cognitive state '{state_name}'",
                               "available": sorted(states.keys())}), 400)
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
            "\n\n---\nTOOLS. You have real tools; they reach the Grid's actual "
            "memory, files, sandbox and the public web:\n"
            "- memory: search_memory, find_contradictions (sweep for 'we said the "
            "opposite once' before a strong claim), cite_source (lane:idx from a "
            "search result -> checkable citation), grid_status (your own vitals)\n"
            "- files: read_file, list_files, write_note, append_note, list_notes\n"
            "- web: web_search then fetch_url to read a result (public sites only)\n"
            "- compute: run_python — CHECK equations by evaluating them instead of "
            "reciting them; print() the result\n"
            "- skills: list_skills, read_skill, save_skill (your own playbook — "
            "check it before improvising, save workflows that worked)\n"
            "- repo: git_status, git_log, git_diff (read-only)\n"
            "- time: current_time, temporal_pattern_scan (is the dreaming cycling "
            "or drifting — UBBM θ autocorrelation)\n"
            "Rules:\n"
            "- Prefer a tool over recall for anything checkable: what a document "
            "says, what the archive holds, what a number evaluates to, today's date.\n"
            "- Never claim to have used a tool you did not call, and never invent "
            "a file's or page's contents. If a tool errors, say so plainly.\n"
            "- Stay in character while using them; the tools serve the voice, not "
            "the other way round."
        )

    return {"message": message, "state_name": state_name, "node": node,
            "do_index": do_index, "c": c, "cloud": cloud, "cconf": cconf,
            "mem_count": mem_count, "model_label": model_label,
            "use_tools": use_tools, "max_rounds": max_rounds,
            "messages": [{"role": "system", "content": sys_content},
                         {"role": "user", "content": user_prompt}]}, None


@app.route("/api/chat", methods=["POST"])
def api_chat():
    ctx, err = _chat_context(request.json or {})
    if err:
        return err
    c, cloud, cconf = ctx["c"], ctx["cloud"], ctx["cconf"]
    message, state_name, node = ctx["message"], ctx["state_name"], ctx["node"]
    mem_count, model_label = ctx["mem_count"], ctx["model_label"]
    use_tools, max_rounds = ctx["use_tools"], ctx["max_rounds"]
    messages, tool_trace = ctx["messages"], []

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

    indexed = _index_exchange(ctx, content)
    return jsonify({"response": content, "model": model_label, "mem_count": mem_count,
                    "indexed": indexed, "tools": tool_trace})


def _index_exchange(ctx: dict, content: str) -> bool:
    """Index the exchange as ONE entry: question and answer stay together, so a
    dream that later surfaces this fragment still knows what was asked.
    (Saving replies alone leaves orphaned answers with no context.)"""
    c, cconf = ctx["c"], ctx["cconf"]
    if not (ctx["do_index"] and bool(cconf.get("index_chat", True))):
        return False
    cap = int(cconf.get("chat_entry_max_chars", 1800))
    message = ctx["message"]
    q = message if len(message) <= cap else message[:cap] + " …"
    a = content if len(content) <= cap else content[:cap] + " …"
    entry = f"CHAT ({ctx['state_name'] or 'node'} · {ctx['node']})\nQ: {q}\n\nA: {a}"
    try:
        r = http.post(f"{bridge(c)}/add_entry",
                      json={"text": entry, "profile": "conversations", "node": ctx["node"],
                            "source": f"Awen Command Deck ({ctx['node']})"}, timeout=60)
        return r.ok and r.json().get("status") == "success"
    except Exception:
        return False


@app.route("/api/chat_stream", methods=["POST"])
def api_chat_stream():
    """The same conversation as /api/chat, on a live wire (Server-Sent Events).

    Every model round streams as it generates. A round that turns out to be
    tool calls announces them (`tool` events) and resets the bubble; the round
    that answers streams token by token. The final event carries the model
    label, per-turn token usage (stream_options.include_usage), the tool trace
    and whether the exchange was indexed. Replies used to land in one lump
    after the full wait — this is idea #5 from IDEAS.md paid off."""
    ctx, err = _chat_context(request.json or {})
    if err:
        return err
    c, cloud = ctx["c"], ctx["cloud"]
    use_tools, max_rounds = ctx["use_tools"], ctx["max_rounds"]

    def sse(obj) -> str:
        return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"

    def generate():
        messages, tool_trace, usage = list(ctx["messages"]), [], None
        try:
            rounds = 0
            while True:
                final = None
                for kind, payload in llm_stream(c, cloud, messages,
                                                tools=TOOL_SPECS if use_tools else None):
                    if kind == "delta":
                        yield sse({"delta": payload})
                    else:
                        final = payload
                usage = (final or {}).get("usage") or usage
                calls = (final or {}).get("tool_calls")
                if not (use_tools and calls) or rounds >= max_rounds:
                    content = (final or {}).get("content") or ""
                    if use_tools and calls and rounds >= max_rounds:
                        # budget spent mid-reach: close the calls and make it speak
                        yield sse({"reset": True, "note": "tool budget spent"})
                        messages.append({"role": "assistant", "content": content or "",
                                         "tool_calls": calls})
                        for call in calls:
                            messages.append({"role": "tool",
                                             "tool_call_id": call.get("id", ""),
                                             "name": (call.get("function") or {}).get("name", ""),
                                             "content": "tool budget spent — answer from what you have"})
                        final2 = None
                        for kind, payload in llm_stream(c, cloud, messages, tools=None):
                            if kind == "delta":
                                yield sse({"delta": payload})
                            else:
                                final2 = payload
                        content = (final2 or {}).get("content") or content
                        usage = (final2 or {}).get("usage") or usage
                    if not content:
                        content = "(empty response — model may have spent its budget thinking)"
                        yield sse({"delta": content})
                    indexed = _index_exchange(ctx, content)
                    yield sse({"done": True, "model": ctx["model_label"],
                               "mem_count": ctx["mem_count"], "indexed": indexed,
                               "tools": tool_trace, "usage": usage})
                    return
                # tool round: wipe whatever the model narrated, run the calls live
                rounds += 1
                yield sse({"reset": True})
                messages.append({"role": "assistant", "content": (final or {}).get("content") or "",
                                 "tool_calls": calls})
                for call in calls:
                    fn = call.get("function") or {}
                    name = str(fn.get("name", ""))
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        args = {}
                    if not isinstance(args, dict):
                        args = {}
                    yield sse({"tool": name,
                               "args": {k: str(v)[:80] for k, v in args.items()}})
                    result = str(run_tool(name, args, c, ctx["node"]))
                    tool_trace.append({"tool": name, "args": args,
                                       "ok": not result.startswith("error:"),
                                       "chars": len(result)})
                    yield sse({"tool_done": name, "ok": not result.startswith("error:")})
                    messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                     "name": name, "content": result[:8000]})
        except Exception as e:
            yield sse({"error": f"LLM stream failed: {e}"})
            yield sse({"done": True, "model": ctx["model_label"],
                       "mem_count": ctx["mem_count"], "indexed": False,
                       "tools": tool_trace, "usage": usage})

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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

