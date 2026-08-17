"""
runtime_vs_spec.py — does the dreaming machine corroborate its own published spec?

Borrowed in method from Aether Scope's RUNTIME_VS_SPEC.md, rebuilt against the
Awen Engine's own data. Three questions, all answerable from what is already on
this disk:

  1. COVERAGE  — which published theorems do the dreams keep independently
                 landing on?
  2. ORPHANS   — which published theorems has the runtime NEVER once surfaced?
  3. EMERGING  — which high-urgency concepts do the dreams keep producing that
                 map to no theorem at all? Those are next-paper candidates.

The thing their version got wrong, and this one fixes: their emerging-terms
list leaks stop-words ('nthe', 'have', 'like', 'through' in the top 30), which
drowns the signal. Here terms are properly tokenised, stop-listed, required to
appear across several distinct dreams (not just repeated inside one), and
weighted by the urgency the engine itself assigned.

Nothing here is a verdict on whether a theorem is TRUE. This measures only what
the runtime talks about. A theorem being an orphan is not evidence against it —
it may simply have no seeds in the corpus.

Run:  py -3.11 runtime_vs_spec.py [--top 30] [--json report.json]
"""

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
THEOREM_CSV = ROOT / "Memory" / "Math" / "01_theorem_index.csv"
THEOREM_CSV_ALT = ROOT / "Memory" / "Math" / "Recursive Harmonic Codex Theorem Index.csv"
LEDGER = ROOT / "knowledge_entries.jsonl"
PING_REPORTS = ROOT / "Ping Reports"
RELAY = ROOT / "cognitive_relay"

TOKEN = re.compile(r"[a-z][a-z0-9\-']{2,}")
URGENCY = re.compile(r"urgency[\s_]*(\d+)[/_](\d+)", re.I)
DREAMID = re.compile(r"dreamid[:\s_]*([0-9a-f]{6,})", re.I)
# The engine's own ping header. Anything without this is not runtime output.
DREAM_SIG = re.compile(r"\[urgency\s*\d+\s*[/_]\s*\d+\]\s*dreamid", re.I)
# The dream cycle prints its own scaffolding into the entry; those words are the
# engine talking about itself, not the insight. Strip them from term counting.
ENGINE_NOISE = set("""
json subject frags pinging inject merge execute broadcast chars entry saved
profile priority score final passed urgent chain node agent relay payload
chunk ledger faiss vector vectors index indices chunkid stdout console
starting cycle mode classic cross domain bisociation leap synthesise synthesize
""".split())

STOP = set("""
the of and to in a is that it for on as with was be by are this or from at an not
but have has had were which their they them its his her our your you we can will
would could should may might must do does did been being if then than when what
who how why all any some more most other into over under such no nor only own
same so too very just also about after before between both during each few
further here there once again against above below off out up down these those am
he she him who whom whose because until while one two three four five six seven
eight nine ten also thus hence therefore however moreover within without upon via
per use used using uses new non pre post etc first second next last many much
several often always never still yet even ever less least well back way make made
making take taken given give gives get got see seen say said know known think
thought find found work works working part parts form forms number numbers system
systems state states point points line lines set sets case cases time times
process processes term terms value values result results based upon toward
towards across around through between among since though although whether
dream dreams dreaming seed seeds fragment fragments insight insights cycle
starting synthesis urgency dreamid knowledge shared research chunk chunks
""".split())


def demojibake(s: str) -> str:
    """The theorem CSV was written cp1252-as-utf8; repair it where possible."""
    if not any(m in s for m in ("Ã", "â€", "Î", "Â")):
        return s
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


def load_theorems():
    src = THEOREM_CSV if THEOREM_CSV.exists() else THEOREM_CSV_ALT
    if not src.exists():
        raise SystemExit(f"No theorem index found at {THEOREM_CSV}")
    out = []
    with open(src, "r", encoding="utf-8", errors="replace", newline="") as f:
        for row in csv.DictReader(f):
            name = demojibake((row.get("Theorem/Finding") or "").strip())
            if not name:
                continue
            eq = demojibake((row.get("Mathematical Identity/Equation") or "").strip())
            sig = demojibake((row.get("Significance") or "").strip())
            # strip the parenthetical alias so "The Ta-Dah Protocol (X)" matches "ta dah protocol"
            base = norm(re.sub(r"\(.*?\)", "", name))
            aliases = {base}
            m = re.search(r"\((.*?)\)", name)
            if m:
                aliases.add(norm(m.group(1)))
            aliases = {a for a in aliases if len(a) > 6}
            # distinctive content words from the name, for a weaker second signal
            keys = {t for t in TOKEN.findall(base) if t not in STOP and len(t) > 4}
            out.append(dict(name=name, eq=eq, sig=sig, aliases=aliases,
                            keys=keys, source=(row.get("Source") or "").strip()))
    # de-duplicate by normalised name, keeping the first
    seen, uniq = set(), []
    for t in out:
        k = norm(re.sub(r"\(.*?\)", "", t["name"]))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(t)
    print(f"  {len(uniq)} theorems ({src.relative_to(ROOT)})")
    return uniq


def load_dreams():
    """Dream insights, wherever they live: the knowledge ledger, Ping Reports, relay."""
    dreams = []

    if LEDGER.exists():
        for line in open(LEDGER, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
            except Exception:
                continue
            if not isinstance(t, str):
                t = t.get("text", "")
            # Must carry the dream cycle's actual signature. Matching on the mere
            # presence of 'dreamid'/'dream insight' pulls in research documents
            # that only TALK about dreams — that false-positive set is what made
            # the first emerging-terms run read like a changelog ('json',
            # 'chars', 'merge'): it was scoring prose about the engine, not
            # output from it.
            if not (DREAM_SIG.search(t) and "dream cycle" in t.lower()):
                continue
            u = URGENCY.search(t)
            d = DREAMID.search(t)
            dreams.append(dict(text=t, urgency=int(u.group(1)) if u else 0,
                               id=d.group(1) if d else f"ledger{len(dreams)}",
                               src="ledger"))

    if PING_REPORTS.exists():
        for p in PING_REPORTS.glob("*.txt"):
            t = p.read_text(encoding="utf-8", errors="replace")
            u = URGENCY.search(p.name) or URGENCY.search(t)
            d = DREAMID.search(p.name) or DREAMID.search(t)
            dreams.append(dict(text=t, urgency=int(u.group(1)) if u else 0,
                               id=d.group(1) if d else p.stem[:18], src="report"))

    if RELAY.exists():
        for p in RELAY.glob("ping_*.json"):
            try:
                j = json.loads(p.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            t = " ".join(str(j.get(k, "")) for k in
                         ("subject", "seed_text", "body_fragments", "synthesis"))
            u = URGENCY.search(str(j.get("urgency", "")))
            dreams.append(dict(text=t, urgency=int(u.group(1)) if u else 0,
                               id=str(j.get("subject", p.stem))[-10:], src="relay"))

    # one dream id can appear as several ledger rows; fold them together
    merged = {}
    for d in dreams:
        m = merged.setdefault(d["id"], dict(text="", urgency=0, id=d["id"], src=d["src"]))
        m["text"] += "\n" + d["text"]
        m["urgency"] = max(m["urgency"], d["urgency"])
    out = list(merged.values())
    print(f"  {len(out)} distinct dreams "
          f"(urgency: max {max((d['urgency'] for d in out), default=0)}, "
          f"median {sorted(d['urgency'] for d in out)[len(out)//2] if out else 0})")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--min-dreams", type=int, default=3,
                    help="a term must appear in this many DISTINCT dreams to count")
    ap.add_argument("--ubiquity", type=float, default=0.30,
                    help="drop terms appearing in more than this fraction of dreams "
                         "(they are letterhead, not signal)")
    ap.add_argument("--json", type=str, default="")
    args = ap.parse_args()

    print("RUNTIME vs SPEC — does the engine corroborate its own papers?\n")
    theorems = load_theorems()
    dreams = load_dreams()
    if not dreams:
        raise SystemExit("\nNo dreams found. Nothing to compare against.")

    normed = [(d, norm(d["text"])) for d in dreams]

    # ---- 1 & 2: coverage and orphans -----------------------------------
    for t in theorems:
        hits, urg = [], 0
        for d, nt in normed:
            if any(a and a in nt for a in t["aliases"]):
                hits.append(d["id"]); urg = max(urg, d["urgency"]); continue
            if len(t["keys"]) >= 2 and sum(1 for k in t["keys"] if k in nt) >= 2:
                hits.append(d["id"]); urg = max(urg, d["urgency"])
        t["hits"] = hits
        t["peak_urgency"] = urg

    covered = sorted([t for t in theorems if t["hits"]],
                     key=lambda t: (-len(t["hits"]), t["name"]))
    orphans = [t for t in theorems if not t["hits"]]

    print(f"\n{'='*78}\n1. COVERAGE — theorems the runtime keeps landing on\n{'='*78}")
    for t in covered[:20]:
        print(f"  {len(t['hits']):>4} dreams  (peak urgency {t['peak_urgency']:>3})  {t['name'][:56]}")
    if not covered:
        print("  none — no theorem name appears in any dream")

    print(f"\n{'='*78}\n2. ORPHANS — published, never once dreamt ({len(orphans)}/{len(theorems)})\n{'='*78}")
    for t in orphans[:28]:
        eq = (t["eq"][:44] + "…") if len(t["eq"]) > 44 else t["eq"]
        print(f"  ✗ {t['name'][:46]:<48} {eq}")
    if len(orphans) > 28:
        print(f"  … and {len(orphans)-28} more")

    # ---- 3: emerging terms ---------------------------------------------
    # Anything already named in the spec is not "emerging".
    spec_vocab = set()
    for t in theorems:
        spec_vocab |= set(TOKEN.findall(norm(t["name"] + " " + t["eq"] + " " + t["sig"])))

    df = Counter()                 # distinct dreams containing the term
    weight = defaultdict(float)    # urgency-weighted mass
    examples = defaultdict(list)
    for d, nt in normed:
        toks = {tok for tok in TOKEN.findall(nt)
                if tok not in STOP and tok not in spec_vocab and tok not in ENGINE_NOISE}
        w = 1.0 + (d["urgency"] / 12.0)     # engine scores urgency out of 12
        for tok in toks:
            df[tok] += 1
            weight[tok] += w
            if len(examples[tok]) < 3:
                examples[tok].append(d["id"])

    # BOILERPLATE GATE. Raw urgency-weighted frequency is worthless here: every
    # ping carries the agent name, the Awen Grid header and the academia.edu
    # links, so 'lumos', 'ngrok', 'academia' top the list while saying nothing.
    # That is the same failure as Aether Scope's stop-word leak wearing a
    # different coat. A term is only a NEXT-PAPER CANDIDATE if it is enriched in
    # a subset of dreams — present often enough to be a pattern, rare enough to
    # be about something. So: drop the near-ubiquitous, then IDF-weight.
    N = len(normed)
    ubiquity_cut = max(2, int(args.ubiquity * N))
    import math
    emerging = []
    for t, n in df.items():
        if n < args.min_dreams or n > ubiquity_cut:
            continue
        idf = math.log(N / n)
        emerging.append((weight[t] * idf, n, t))
    emerging.sort(reverse=True)
    dropped = sum(1 for t, n in df.items() if n > ubiquity_cut)

    print(f"\n{'='*78}\n3. EMERGING — concepts in NO theorem, enriched not ubiquitous\n{'='*78}")
    print(f"  term must appear in >= {args.min_dreams} and <= {ubiquity_cut} of {N} dreams,")
    print(f"  scored by (engine urgency x IDF). {dropped} ubiquitous terms dropped as")
    print(f"  ping boilerplate (agent names, headers, profile links).\n")
    print(f"  {'score':>7} {'dreams':>7}  term")
    for w, n, t in emerging[:args.top]:
        print(f"  {w:>7.1f} {n:>7}  {t}")
    if not emerging:
        print("  nothing clears the threshold — try --min-dreams 2")

    print(f"\n{'='*78}\nSUMMARY\n{'='*78}")
    print(f"  theorems indexed      {len(theorems)}")
    print(f"  dreams analysed       {len(dreams)}")
    print(f"  corroborated          {len(covered)}  ({100*len(covered)/max(1,len(theorems)):.0f}%)")
    print(f"  orphaned              {len(orphans)}  ({100*len(orphans)/max(1,len(theorems)):.0f}%)")
    print(f"  emerging candidates   {len(emerging)}")
    print("\n  An orphan is not a refutation — it means the corpus gave the dream")
    print("  cycle no seeds for it. Read it as a gap in coverage, not in truth.")

    if args.json:
        out = dict(
            generated=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            theorems=len(theorems), dreams=len(dreams),
            covered=[dict(name=t["name"], dreams=len(t["hits"]),
                          peak_urgency=t["peak_urgency"]) for t in covered],
            orphans=[dict(name=t["name"], equation=t["eq"]) for t in orphans],
            emerging=[dict(term=t, dreams=n, weight=round(w, 2),
                           examples=examples[t]) for w, n, t in emerging[:args.top]],
        )
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        print(f"\n  wrote {args.json}")


if __name__ == "__main__":
    main()
