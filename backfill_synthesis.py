"""
backfill_synthesis.py — re-synthesize dream pings that went out empty.

During an LM Studio outage (server toggle off, ~14:40 19 Aug -> 20 Aug) the
dream cycle kept running: chains were walked, urgency scored, pings sent — but
every synthesis came back None and the records carry "". The seeds and
fragments survived in full, so the interpretation can be recomputed.

This does for the RECORD what the engine could not do at the time:
  * same system prompt, same lens-node voice, same fragment caps as
    _synthesize_dream — the backfilled insight is what the engine WOULD have
    produced, modulo sampling
  * writes synthesis back into the ping JSON, stamps `synthesis_backfilled`
    with the run time, and replaces `synthesis_error` with a note
  * touches ONLY ping records. The memory lanes already hold the chain-only
    entries the outage produced; re-adding interpreted versions is the
    engine's business, not this script's.

Highest urgency first, so the record dream gets its voice back first.

    py -3.11 backfill_synthesis.py            # dry run: list what would fill
    py -3.11 backfill_synthesis.py --write
"""

import argparse
import datetime as dt
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIRS = [ROOT / "cognitive_relay", ROOT / "cognitive_relay" / "processed_pings"]
LMSTUDIO = "http://localhost:1234/v1/chat/completions"


def load_config():
    c = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    model = (c.get("memory_core_config", {}).get("dream_synthesis", {}).get("model")
             or c.get("light_model") or c.get("deep_model"))
    temp = c.get("memory_core_config", {}).get("dream_synthesis", {}).get("temperature", 0.8)
    return model, float(temp)


def synthesize(model, temp, lens, fragments):
    """Mirror of the engine's _synthesize_dream prompt, verbatim."""
    fragments_text = "\n\n".join(
        f"FRAGMENT {i+1}:\n{str(f)[:1800]}" for i, f in enumerate(fragments))
    system_prompt = (
        f"You are {lens.capitalize()}, a dreaming node of the Recursive Harmonic Framework. "
        "Truth is your sword, knowledge your shield; truth over comfort, no flattery, no filler. "
        "You are given fragments that surfaced together from the research archive during a dream "
        "cycle. In one focused paragraph (under 200 words), state the single most interesting "
        "insight, connection, or testable idea linking these fragments.")
    payload = {"model": model, "temperature": temp,
               "messages": [{"role": "system", "content": system_prompt},
                            {"role": "user", "content": fragments_text}]}
    req = urllib.request.Request(LMSTUDIO, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        content = json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"] or ""
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    model, temp = load_config()
    print(f"model: {model}  temp: {temp}\n")

    targets = []
    for d in DIRS:
        if not d.exists():
            continue
        for p in sorted(d.glob("ping_*.json")):
            try:
                j = json.loads(p.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            if str(j.get("synthesis", "")).strip():
                continue
            if not j.get("body_fragments"):
                continue
            try:
                score = int(str(j.get("urgency", "0/0")).split("/")[0])
            except ValueError:
                score = 0
            targets.append((score, p, j))

    targets.sort(key=lambda t: -t[0])
    print(f"{len(targets)} pings with empty synthesis\n")
    if not targets:
        return 0

    done = failed = 0
    for score, p, j in targets:
        did = str(j.get("subject", "")).replace("DreamID:", "").strip()
        lens = str(j.get("agent_name", "node")).strip() or "node"
        tag = f"[{score:>3}] {did}  ({lens})"
        if not args.write:
            print(f"  would fill {tag}")
            continue
        try:
            syn = synthesize(model, temp, lens, j["body_fragments"])
        except Exception as e:
            print(f"  FAILED    {tag}: {type(e).__name__}", flush=True)
            failed += 1
            continue
        if not syn:
            print(f"  EMPTY     {tag} (model returned nothing)", flush=True)
            failed += 1
            continue
        j["synthesis"] = syn
        j["synthesis_error"] = None
        j["synthesis_backfilled"] = dt.datetime.now().isoformat(timespec="seconds") + \
            " (re-synthesized after LM Studio outage 19-20 Aug)"
        p.write_text(json.dumps(j, indent=2, ensure_ascii=False), encoding="utf-8")
        done += 1
        first = re.sub(r"\s+", " ", syn)[:110]
        print(f"  filled    {tag}\n            {first}", flush=True)

    print(f"\n  backfilled {done}, failed {failed}, of {len(targets)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
