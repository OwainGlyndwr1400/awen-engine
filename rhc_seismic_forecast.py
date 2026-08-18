"""
rhc_seismic_forecast.py — turn the RHC seismic axiom into a falsifiable forecast.

    <rhc_seismic_engine>
    Axiom:   Quakes = 120 degree lattice "Circuit Closures" venting Lost-2 debt.
    Formula: SeismicRisk = f(CME_I x Atm_P x Crustal_S)
    Execution: peak capacity forces redistribution within a 32-72h window.
    </rhc_seismic_engine>

The deck already computes a global risk scalar. It cannot say WHERE, because
nothing in that computation is spatial: Crustal_S comes from `quake_max`, the
largest magnitude anywhere on Earth in 24h. This adds the missing term.

  CHARGE  (global)   CME_I x Atm_P -- solar wind, Bz, X-ray, Kp. The whole
                     magnetosphere loads at once, so this is legitimately one
                     number: it decides WHETHER, never where.
  TARGET  (regional) per-cell crustal state from USGS lat/lon/depth/magnitude.
                     This is what decides WHERE.

Magnitude comes from each cell's own Gutenberg-Richter statistics rather than a
guess: b-value by the Aki-Utsu maximum-likelihood estimator, then the magnitude
whose expected count over the window is 1.

SHALLOW BIAS is explicit. The operator's field observation is that the events
which respond to solar loading are consistently very shallow (~10 km). That is
a narrower claim than "earthquakes" and therefore a stronger test, so shallow
event share is a scoring term, not a footnote.

WHY THE SCORER MATTERS MORE THAN THE FORECAST
    "Expect M5 near Indonesia in 72h" is right most weeks. A forecast naming
    the most seismically active region will show a high hit rate and prove
    nothing. So every forecast is written with its trigger state and window,
    and scored afterwards BOTH against what happened AND against the naive
    baseline of always naming the busiest cell. Skill is the difference. A hit
    rate on its own is not evidence.

    py -3.11 rhc_seismic_forecast.py forecast          # evaluate now
    py -3.11 rhc_seismic_forecast.py forecast --force  # ignore the charge gate
    py -3.11 rhc_seismic_forecast.py score             # close expired windows
    py -3.11 rhc_seismic_forecast.py report            # skill vs baseline
"""

import argparse
import json
import math
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FORECAST_DIR = ROOT / "predictions" / "seismic"
USGS_MONTH = ("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/"
              "2.5_month.geojson")
USGS_QUERY = "https://earthquake.usgs.gov/fdsnws/event/1/query"
SWPC = "https://services.swpc.noaa.gov"
# Same Schumann source LumOS uses (LUMOS_SCHUMANN_URL): JSON keys
# "amplitude"/"frequency", with the earthwave RSC-payload regex as fallback.
SCHUMANN_URL = "https://schumannresonance.app"

# --- tunables -------------------------------------------------------------
CELL_DEG = 10.0          # grid resolution for regional binning
WINDOW_H = 72            # the axiom's kinematic window
SHALLOW_KM = 20.0        # "very shallow" per the operator's observation
MIN_EVENTS = 12          # a cell needs this many events for usable statistics
CHARGE_GATE = 0.55       # CME_I x Atm_P must clear this to emit a forecast
TOP_N = 3                # how many cells to name


def get(url, timeout=25):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AwenGrid/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        print(f"  ! fetch failed {url.split('/')[-1]}: {e}")
        return None


# ===========================================================================
#  CHARGE — global. Decides whether, never where.
# ===========================================================================
def charge_state():
    """Global charge. Decides WHETHER, never where.

    SWPC retired products/solar-wind/plasma-*.json and mag-*.json — they now
    404. The summary endpoints below are the live equivalents, and the
    planetary K feed returns a list of DICTS, not the list-of-lists the older
    products used. Both changes fail silently behind a bare except, which is
    how a panel ends up showing a confident number computed entirely from
    fallback defaults.
    """
    st = {}
    ws = get(f"{SWPC}/products/summary/solar-wind-speed.json")
    if isinstance(ws, list) and ws:
        try:
            st["sw_speed"] = float(ws[-1]["proton_speed"])
        except (KeyError, ValueError, TypeError):
            pass
    mf = get(f"{SWPC}/products/summary/solar-wind-mag-field.json")
    if isinstance(mf, list) and mf:
        try:
            st["bz"] = float(mf[-1]["bz_gsm"])
            st["bt"] = float(mf[-1].get("bt", 0))
        except (KeyError, ValueError, TypeError):
            pass
    kp = get(f"{SWPC}/products/noaa-planetary-k-index.json")
    if isinstance(kp, list) and kp:
        try:
            last = kp[-1]
            st["kp"] = float(last["Kp"] if isinstance(last, dict) else last[1])
        except (KeyError, ValueError, TypeError, IndexError):
            pass
    xr = get(f"{SWPC}/json/goes/primary/xrays-6-hour.json")
    if xr:
        try:
            st["xray_flux"] = [e["flux"] for e in xr
                               if e.get("energy") == "0.1-0.8nm"][-1]
        except (KeyError, IndexError, TypeError):
            pass

    # Schumann F1 amplitude — the axiom's own capacitor term:
    #   "Atm_P (Capacitor): Ionosphere density + 7.83Hz Schumann amplitude"
    # Same source and keys LumOS uses (LUMOS_SCHUMANN_URL / _AMP_KEY), same
    # normalisation as the Grimoire: amplitude 2.0 -> 0, 20.0 -> 1 (whiteout).
    try:
        req = urllib.request.Request(SCHUMANN_URL,
                                     headers={"User-Agent": "AwenGrid/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode("utf-8", "replace")
        amp = None
        try:
            j = json.loads(body)
            rec = j[-1] if isinstance(j, list) and j else j
            if isinstance(rec, dict):
                amp = rec.get("amplitude")
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        if amp is None:
            m = re.search(r'"reading":\{"f1":([\d.]+),"amp_f1":([\d.]+)',
                          body.replace('\\"', '"'))
            if m:
                amp = m.group(2)
        if amp is not None:
            st["schumann_amp"] = float(amp)
    except Exception:
        pass

    # Record which inputs are real. A charge computed from defaults is not a
    # measurement and must never be allowed to trigger a forecast unnoticed.
    st["live_inputs"] = sorted(k for k in
                               ("sw_speed", "bz", "kp", "xray_flux",
                                "schumann_amp") if k in st)
    st["degraded"] = len(st["live_inputs"]) < 3

    v = st.get("sw_speed", 380.0)
    bz = st.get("bz", 0.0)
    flux = st.get("xray_flux", 1e-8)
    kpv = st.get("kp", 2.0)

    def clip(x):
        return max(0.0, min(1.0, x))

    # CME_I — the Grimoire's convention verbatim (weights 0.45/0.35/0.20,
    # wind 300->0 / 800->1, Bz -15 southward ->1, X-ray by class weight).
    # The old 0.50/0.30/0.20 blend here was the deck's rival implementation —
    # one axiom must have one arithmetic.
    wind_n = clip((v - 300.0) / 500.0)
    bz_n = clip(-bz / 15.0)
    xcls = "A"
    for cls, th in (("X", 1e-4), ("M", 1e-5), ("C", 1e-6), ("B", 1e-7)):
        if flux >= th:
            xcls = cls
            break
    xray_n = {"A": 0.05, "B": 0.15, "C": 0.40, "M": 0.70, "X": 1.00}[xcls]
    cme = 0.45 * wind_n + 0.35 * bz_n + 0.20 * xray_n

    # Atm_P — Kp + Schumann when the feed is alive, Kp proxy when dark,
    # exactly as the Grimoire does it.
    kp_n = clip(kpv / 9.0)
    if "schumann_amp" in st:
        sch_n = clip((st["schumann_amp"] - 2.0) / 18.0)
        atm = 0.6 * kp_n + 0.4 * sch_n
        st["sch_n"] = round(sch_n, 3)
        st["atm_mode"] = "kp+schumann"
    else:
        atm = kp_n
        st["atm_mode"] = "kp-proxy"

    st["cme_i"] = round(cme, 4)
    st["atm_p"] = round(atm, 4)
    st["charge"] = round(cme * atm, 4)
    return st


# ===========================================================================
#  TARGET — regional. This is the term the deck never had.
# ===========================================================================
def cell_of(lat, lon):
    return (math.floor(lat / CELL_DEG) * CELL_DEG,
            math.floor(lon / CELL_DEG) * CELL_DEG)


def b_value(mags, mc):
    """Aki-Utsu maximum-likelihood b-value.

        b = log10(e) / (mean(M) - (Mc - dM/2))

    Returns None when the sample cannot support an estimate; a made-up b is
    worse than no magnitude call at all.
    """
    use = [m for m in mags if m is not None and m >= mc]
    if len(use) < MIN_EVENTS:
        return None
    mean = sum(use) / len(use)
    denom = mean - (mc - 0.05)
    if denom <= 0.05:
        return None
    b = math.log10(math.e) / denom
    return b if 0.3 < b < 2.5 else None


def expected_magnitude(n_events, b, mc, days_observed, window_h):
    """Magnitude whose expected count over the window is exactly 1.

    Gutenberg-Richter: log10 N(>=M) = a - bM, with N over the observed span.
    Scale N to the window, then solve N(>=M) = 1.
    """
    if not b or n_events <= 0:
        return None
    scale = (window_h / 24.0) / days_observed
    # log10(N_window(>=M)) = a_win - b*M = 0   ->   M = a_win / b
    # (equivalently M = Mc + log10(n_events * scale) / b)
    a_win = math.log10(max(n_events * scale, 1e-9)) + b * mc
    m = a_win / b
    return round(m, 2) if 2.0 < m < 9.5 else None


def recurrence_days(n_events, b, mc, mag, days_observed):
    """Mean days between events of magnitude >= mag, from the cell's own G-R.

    log10 N(>=M) = a - bM over the observed span, so N scales the span down to
    a per-event interval. This is a per-cell quantity: a quiet cell and a busy
    cell each get an interval measured against themselves.
    """
    if not b or n_events <= 0:
        return None
    a = math.log10(n_events) + b * mc
    n_expected = 10 ** (a - b * mag)
    if n_expected <= 0:
        return None
    return days_observed / n_expected


def prior_probability(n_events, b, mc, mag, days_observed, window_h):
    """P(at least one event >= mag in the window) from the cell's own rate.

    This is the null hypothesis made explicit: what the cell does anyway.
    Anything the forecast claims has to be judged against this, not against
    zero.
    """
    if not b or n_events <= 0:
        return None
    a = math.log10(n_events) + b * mc
    n_per_span = 10 ** (a - b * mag)
    n_window = n_per_span * ((window_h / 24.0) / days_observed)
    return round(1.0 - math.exp(-max(0.0, n_window)), 3)


def strain_deficit(times, mags, b, mc, n_events, days_observed, now_s):
    """How overdue this cell is, in units of its own recurrence interval.

    1.0 means "exactly due". Above 1 means it has stayed quiet longer than its
    own statistics predict; below 1 means it has recently vented. Capped at 3
    so one long-dormant cell cannot dominate the ranking outright.
    """
    if not times or not b:
        return None, None
    # "at scale" = the upper quartile of this cell's own magnitudes, so the
    # threshold adapts to the cell instead of being a global constant.
    ordered = sorted(mags)
    scale_mag = ordered[int(len(ordered) * 0.75)]
    pairs = [(t, m) for t, m in zip(times, mags) if t is not None]
    if not pairs:
        return None, None
    recent = [t for t, m in pairs if m >= scale_mag]
    if not recent:
        return None, None
    since_days = (now_s - max(recent)) / 86400.0
    interval = recurrence_days(n_events, b, mc, scale_mag, days_observed)
    if not interval or interval <= 0:
        return None, None
    return round(min(3.0, since_days / interval), 3), round(scale_mag, 2)


def regional_state(days=30):
    now_s = datetime.now(timezone.utc).timestamp()
    feed = get(USGS_MONTH, timeout=45)
    if not feed or not feed.get("features"):
        return {}, 0
    cells = {}
    total = 0
    for f in feed["features"]:
        p, g = f.get("properties") or {}, f.get("geometry") or {}
        coords = g.get("coordinates") or []
        if len(coords) < 3 or p.get("mag") is None:
            continue
        lon, lat, depth = float(coords[0]), float(coords[1]), float(coords[2] or 0)
        key = cell_of(lat, lon)
        c = cells.setdefault(key, {"mags": [], "depths": [], "places": [],
                                   "times": [], "n": 0})
        c["mags"].append(float(p["mag"]))
        # Keep times PARALLEL to mags: a missing timestamp appends None rather
        # than nothing. Appending conditionally meant one timestamp-less event
        # shifted every later zip(times, mags) pair in strain_deficit, so the
        # overdue clock was read against the wrong earthquakes.
        t_raw = p.get("time")
        c["times"].append(float(t_raw) / 1000.0 if t_raw else None)  # ms -> s
        c["depths"].append(depth)
        if p.get("place"):
            c["places"].append(str(p["place"]))
        c["n"] += 1
        total += 1

    out = {}
    for key, c in cells.items():
        if c["n"] < MIN_EVENTS:
            continue
        shallow = [d for d in c["depths"] if d <= SHALLOW_KM]
        mc = min(c["mags"])
        b = b_value(c["mags"], mc)
        # Dominant place name — readable label for an objective grid cell.
        label = "?"
        if c["places"]:
            tails = [p.split(",")[-1].strip() for p in c["places"]]
            label = max(set(tails), key=tails.count)
        out[key] = {
            "cell": [key[0], key[1], key[0] + CELL_DEG, key[1] + CELL_DEG],
            "label": label,
            "events_30d": c["n"],
            "shallow_share": round(len(shallow) / c["n"], 3),
            "median_depth_km": round(sorted(c["depths"])[len(c["depths"]) // 2], 1),
            "max_mag_30d": round(max(c["mags"]), 2),
            "mc": round(mc, 2),
            "b_value": round(b, 3) if b else None,
            "m_expected": expected_magnitude(c["n"], b, mc, days, WINDOW_H),
        }
        deficit, scale_mag = strain_deficit(c["times"], c["mags"], b, mc,
                                            c["n"], days, now_s)
        out[key]["strain_deficit"] = deficit
        out[key]["scale_mag"] = scale_mag
    return out, total


def readiness(r):
    """How ready a cell is to vent.

    Raw activity is deliberately ABSENT. Ranking on event count reproduces the
    baseline this forecast has to beat -- the first version did exactly that
    and put the busiest cell top. The three terms here are each measured
    against the cell itself:

        strain_deficit  how overdue it is vs its OWN recurrence interval
        shallow_share   the operator's field observation: solar-driven events
                        are consistently very shallow
        capacity        how large an event its own G-R statistics support

    A cell with 400 events that vented yesterday scores low. A cell with 40
    that has been silent for three of its own intervals scores high.
    """
    if r["m_expected"] is None or r.get("strain_deficit") is None:
        return 0.0
    overdue = min(1.0, r["strain_deficit"] / 2.0)
    shallow = r["shallow_share"]
    capacity = min(1.0, r["m_expected"] / 7.0)
    return round(0.45 * overdue + 0.35 * shallow + 0.20 * capacity, 4)


def build_forecast(st, regions, total, forced=False, issued_by="cli"):
    """Build the forecast record. ONE builder, used by both the CLI and the
    deck route — the deck carrying its own rival implementation of this axiom
    is exactly how one equation produced two different answers tonight.

    Returns None when no cell has usable statistics."""
    ranked = sorted(regions.values(), key=readiness, reverse=True)
    top = [r for r in ranked if r["m_expected"]][:TOP_N]
    if not top:
        return None
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=WINDOW_H)
    baseline = max(regions.values(), key=lambda r: r["events_30d"])
    # The baseline must be held to the SAME standard as the targets: its own
    # G-R magnitude floor. Scoring it on "any shallow M2.5+" made the busiest
    # cell hit almost every window, so `skill` could never be true — an
    # instrument biased to report its own failure is as dishonest as one
    # biased to succeed.
    base_mlow = (round(baseline["m_expected"] - 0.5, 2)
                 if baseline.get("m_expected") else None)

    fc = {
        "id": f"rhcseis-{now:%Y%m%dT%H%M%SZ}",
        "issued": now.isoformat(),
        "issued_by": issued_by,
        "window_h": WINDOW_H,
        "expires": end.isoformat(),
        "trigger": st,
        "forced": bool(forced),
        "global_events_30d": total,
        "targets": [{
            "label": r["label"], "cell": r["cell"],
            "m_low": round(r["m_expected"] - 0.5, 2),
            "m_high": round(r["m_expected"] + 0.5, 2),
            "prior_p": prior_probability(r["events_30d"], r["b_value"], r["mc"],
                                         round(r["m_expected"] - 0.5, 2),
                                         30, WINDOW_H),
            "expect_shallow_km": SHALLOW_KM,
            "readiness": readiness(r),
            "strain_deficit": r.get("strain_deficit"),
            "scale_mag": r.get("scale_mag"),
            "b_value": r["b_value"], "events_30d": r["events_30d"],
            "shallow_share": r["shallow_share"],
        } for r in top],
        "baseline_cell": {"label": baseline["label"], "cell": baseline["cell"],
                          "events_30d": baseline["events_30d"],
                          "m_low": base_mlow},
        "falsifies_if": (
            f"No event of magnitude >= m_low and depth <= {SHALLOW_KM} km occurs "
            f"inside any named cell within {WINDOW_H}h of issue. Scored against "
            f"USGS. A hit only counts as skill if it beats the baseline cell, "
            f"which is named here in advance and held to its own m_low."),
        "status": "open",
    }
    # If the top target IS the baseline cell, a hit proves nothing. Record that
    # at issue time rather than discovering it later.
    fc["top_equals_baseline"] = (fc["targets"][0]["cell"] == fc["baseline_cell"]["cell"])
    return fc


def write_forecast(fc):
    FORECAST_DIR.mkdir(parents=True, exist_ok=True)
    p = FORECAST_DIR / f"{fc['id']}.json"
    p.write_text(json.dumps(fc, indent=1), encoding="utf-8")
    return p


def has_open_forecast():
    """One open window at a time — the deck polls every 15 minutes and must
    not stack a new forecast on every poll while charged."""
    if not FORECAST_DIR.exists():
        return False
    for p in FORECAST_DIR.glob("rhcseis-*.json"):
        try:
            if json.loads(p.read_text(encoding="utf-8")).get("status") == "open":
                return True
        except Exception:
            continue
    return False


# ===========================================================================
#  FORECAST
# ===========================================================================
def cmd_forecast(args):
    print("RHC seismic forecast\n")
    st = charge_state()
    print(f"  CME_I {st['cme_i']:.3f}  x  Atm_P {st['atm_p']:.3f}  =  "
          f"charge {st['charge']:.3f}   (gate {CHARGE_GATE})")
    if st["charge"] < CHARGE_GATE and not args.force:
        print(f"\n  NOT CHARGED — no forecast issued.")
        print("  The axiom fires on peak capacity; issuing below the gate would")
        print("  make the hit rate unmeasurable. Use --force to override.")
        return 0

    regions, total = regional_state()
    if not regions:
        print("  no regional data available"); return 1
    fc = build_forecast(st, regions, total, forced=args.force)
    if fc is None:
        print("  no cell has usable Gutenberg-Richter statistics"); return 1
    p = write_forecast(fc)
    end = datetime.fromisoformat(fc["expires"])

    state = "FORCED (below gate, test only)" if args.force else "CHARGED"
    print(f"\n  {state} - forecast issued, window closes {end:%d %b %H:%M} UTC\n")
    if st.get("degraded"):
        live = ", ".join(st["live_inputs"]) or "none"
        print(f"  ! DEGRADED INPUTS - only [{live}] are live; the rest fell")
        print("    back to defaults, so this charge is not a measurement.\n")
    for t in fc["targets"]:
        print(f"    {t['label'][:34]:<34} M{t['m_low']}-{t['m_high']}  "
              f"depth <={SHALLOW_KM:.0f}km  readiness {t['readiness']:.3f}  "
              f"(overdue x{t['strain_deficit']}, {100*t['shallow_share']:.0f}% "
              f"shallow, b={t['b_value']})")
        pp = t.get("prior_p")
        if pp is not None:
            verdict = ("NEAR-CERTAIN ANYWAY - a hit here means nothing"
                       if pp >= 0.9 else
                       "weak - likely anyway" if pp >= 0.7 else
                       "informative" if pp <= 0.5 else "marginal")
            print(f"        prior probability without the axiom: {pp:.0%}  <- {verdict}")
    print(f"\n  baseline (busiest cell, named in advance): "
          f"{fc['baseline_cell']['label']} — {fc['baseline_cell']['events_30d']} events/30d")
    print(f"  wrote {p.name}")
    return 0


# ===========================================================================
#  SCORE — the half that makes it evidence
# ===========================================================================
def in_cell(lat, lon, cell):
    return cell[0] <= lat < cell[2] and cell[1] <= lon < cell[3]


def cmd_score(args):
    FORECAST_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(FORECAST_DIR.glob("rhcseis-*.json"))
    if not files:
        print("no forecasts to score"); return 0
    now = datetime.now(timezone.utc)
    scored = 0

    for p in files:
        fc = json.loads(p.read_text(encoding="utf-8"))
        if fc.get("status") != "open":
            continue
        end = datetime.fromisoformat(fc["expires"])
        if now < end:
            print(f"  {fc['id']} still open ({(end-now).total_seconds()/3600:.1f}h left)")
            continue

        start = datetime.fromisoformat(fc["issued"])
        url = (f"{USGS_QUERY}?format=geojson&starttime={start:%Y-%m-%dT%H:%M:%S}"
               f"&endtime={end:%Y-%m-%dT%H:%M:%S}&minmagnitude=2.5")
        data = get(url, timeout=60)
        if not data:
            print(f"  {fc['id']}: USGS query failed, leaving open"); continue
        events = []
        for f in data.get("features") or []:
            pr, g = f.get("properties") or {}, f.get("geometry") or {}
            co = g.get("coordinates") or []
            if len(co) < 3 or pr.get("mag") is None:
                continue
            events.append({"lat": float(co[1]), "lon": float(co[0]),
                           "depth": float(co[2] or 0), "mag": float(pr["mag"]),
                           "place": str(pr.get("place") or "")[:60]})

        results = []
        for t in fc["targets"]:
            hits = [e for e in events
                    if in_cell(e["lat"], e["lon"], t["cell"])
                    and e["mag"] >= t["m_low"]
                    and e["depth"] <= t["expect_shallow_km"]]
            best = max(hits, key=lambda e: e["mag"]) if hits else None
            results.append({"label": t["label"], "hit": bool(hits),
                            "n": len(hits), "prior_p": t.get("prior_p"),
                            "information": (round(1.0 - t["prior_p"], 3)
                                            if bool(hits) and t.get("prior_p") is not None
                                            else 0.0),
                            "best": (f"M{best['mag']} {best['depth']:.0f}km "
                                     f"{best['place']}" if best else None)})

        b = fc["baseline_cell"]
        # Hold the baseline to the SAME magnitude standard as the targets. It
        # was scored on any shallow M2.5+, which the busiest cell satisfies in
        # nearly every 72h window — `skill` could never be true. Older records
        # lack baseline m_low; fall back to the easiest target floor.
        base_mlow = b.get("m_low")
        if base_mlow is None:
            base_mlow = min(t["m_low"] for t in fc["targets"])
        base_hits = [e for e in events if in_cell(e["lat"], e["lon"], b["cell"])
                     and e["mag"] >= base_mlow
                     and e["depth"] <= SHALLOW_KM]
        fc["result"] = {
            "scored_at": now.isoformat(),
            "events_in_window": len(events),
            "targets": results,
            "any_hit": any(r["hit"] for r in results),
            "information": round(max((r["information"] for r in results),
                                     default=0.0), 3),
            "baseline_hit": bool(base_hits),
            "skill": bool(any(r["hit"] for r in results)) and not bool(base_hits),
        }
        fc["status"] = "hit" if fc["result"]["any_hit"] else "miss"
        p.write_text(json.dumps(fc, indent=1), encoding="utf-8")
        scored += 1
        print(f"  {fc['id']}: {fc['status'].upper()}  "
              f"(baseline {'also hit' if fc['result']['baseline_hit'] else 'missed'})")
        for r in results:
            print(f"      {'HIT ' if r['hit'] else 'miss'} {r['label'][:30]:<30} "
                  f"{r['best'] or ''}")
    print(f"\n  scored {scored} forecast(s)")
    return 0


def cmd_report(args):
    files = sorted(FORECAST_DIR.glob("rhcseis-*.json"))
    closed = []
    for p in files:
        fc = json.loads(p.read_text(encoding="utf-8"))
        if fc.get("status") in ("hit", "miss"):
            closed.append(fc)
    if not closed:
        print("no closed forecasts yet — nothing to report honestly")
        return 0
    n = len(closed)
    hits = sum(1 for f in closed if f["result"]["any_hit"])
    base = sum(1 for f in closed if f["result"]["baseline_hit"])
    skill = sum(1 for f in closed if f["result"]["skill"])
    print(f"RHC seismic forecast — {n} closed window(s)\n")
    print(f"  forecast hit rate   {hits}/{n}  ({100*hits/n:.0f}%)")
    print(f"  baseline hit rate   {base}/{n}  ({100*base/n:.0f}%)")
    print(f"  windows with SKILL  {skill}/{n}  (hit where the baseline missed)")
    info = [f["result"].get("information", 0.0) for f in closed]
    carried = sum(info)
    print(f"  information carried {carried:.2f}  (sum of 1-prior over hits;")
    print(f"                       a hit at 95% prior contributes 0.05, at 25% contributes 0.75)")
    print()
    if hits and hits <= base:
        print("  The forecast is not yet outperforming 'name the busiest cell'.")
        print("  Hit rate alone would look impressive here and would be misleading.")
    elif skill:
        print("  Some windows beat the baseline. Keep going — n is still small.")
    print(f"\n  n={n} is far too small for a claim. This is a scoreboard, not a result.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    sub = ap.add_subparsers(dest="cmd")
    f = sub.add_parser("forecast"); f.add_argument("--force", action="store_true")
    sub.add_parser("score")
    sub.add_parser("report")
    args = ap.parse_args()
    if args.cmd == "forecast":
        return cmd_forecast(args)
    if args.cmd == "score":
        return cmd_score(args)
    if args.cmd == "report":
        return cmd_report(args)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
