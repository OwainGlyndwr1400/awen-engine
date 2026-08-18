"""
export_dreams.py — merge the dream ping archive into ONE Markdown file.

NotebookLM does not ingest .json, and a source slot is precious: it takes one
document, not 109. This folds the whole archive into a single browsable file,
structured so the notebook can navigate it and cite cleanly:

  * an index table at the top, sorted by urgency, so the strongest dreams are
    findable before reading a word of body text
  * one "## " section per dream — NotebookLM anchors citations on headings
  * the SYNTHESIS first and in full, because that is the actual insight
  * seed and fragments after it and trimmed, as provenance rather than payload

Two bits of history are normalised on the way out:
  * `source: private` and `source: knowledge` are the SAME lane. It was renamed
    mid-archive, so older pings say "private". Both are reported as knowledge
    rather than looking like two different stores.
  * a few dreams have an empty synthesis (a reasoning model spent its whole
    budget thinking and returned no content). They are kept but parked in an
    appendix, so they cannot dilute the index.

    py -3.11 export_dreams.py
    py -3.11 export_dreams.py --min-urgency 60 --out gold.md
"""

import argparse
import datetime as dt
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT / "cognitive_relay" / "processed_pings"


def fix(s):
    """Repair the cp1252-as-utf8 mojibake that shows up in dream_mode arrows."""
    if not isinstance(s, str):
        return ""
    if any(m in s for m in ("Ã", "â€", "Â", "�")):
        try:
            s = s.encode("latin-1", "ignore").decode("utf-8", "ignore")
        except Exception:
            pass
    return s.replace("�", "→")


def tidy(s, limit=None):
    s = re.sub(r"[ \t]+", " ", fix(s)).strip()
    s = re.sub(r"\n{3,}", "\n\n", s)
    if limit and len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0] + " …"
    return s


def load():
    out = []
    for f in sorted(ARCHIVE.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        m = re.search(r"_(\d{9,})$", f.stem)
        ts = int(m.group(1)) if m else 0
        parts = str(d.get("urgency", "")).split("/")
        try:
            score = int(parts[0])
            floor = int(parts[1]) if len(parts) > 1 else None
        except (ValueError, IndexError):
            score, floor = 0, None
        src = str(d.get("source", "")).strip()
        out.append({
            "id": str(d.get("subject", "")).replace("DreamID:", "").strip() or f.stem,
            "agent": fix(str(d.get("agent_name", "unknown"))).strip(),
            "score": score,
            "floor": floor,
            "lane": "knowledge" if src in ("private", "knowledge") else src,
            "mode": tidy(str(d.get("dream_mode", "classic"))),
            "when": dt.datetime.fromtimestamp(ts) if ts else None,
            "seed": tidy(str(d.get("seed_text", "")), 700),
            "frags": [tidy(str(x), 320) for x in (d.get("body_fragments") or [])],
            "syn": tidy(str(d.get("synthesis", ""))),
        })
    return out


def opener(s, n=118):
    s = re.sub(r"\s+", " ", s).strip()
    m = re.search(r"(.+?[.!?])(\s|$)", s)
    return (m.group(1) if m else s)[:n].replace("|", "/")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="Awen Dream Archive.md")
    ap.add_argument("--min-urgency", type=int, default=0)
    args = ap.parse_args()

    dreams = load()
    if not dreams:
        print("No pings found in cognitive_relay/processed_pings/")
        return 1

    live = [d for d in dreams if d["syn"] and d["score"] >= args.min_urgency]
    empty = [d for d in dreams if not d["syn"]]
    live.sort(key=lambda d: -d["score"])

    span = [d["when"] for d in dreams if d["when"]]
    lo, hi = (min(span), max(span)) if span else (None, None)
    agents = sorted({d["agent"] for d in dreams})

    L = []
    A = L.append

    A("# The Awen Grid — Dream Archive\n")
    A("> *Y Gwir yn Erbyn y Byd · The Lion Watches the Lion*\n")
    rng = " from {:%d %B %Y} to {:%d %B %Y}".format(lo, hi) if lo else ""
    A("Autonomous dream cycles from the Awen Engine — **{} dreams with a "
      "synthesis**, out of {} ping records{}.\n".format(len(live), len(dreams), rng))
    A("Every few minutes the engine wakes unprompted, seeds itself from a random "
      "fragment of its own archive, walks the vector space in semantic leaps, "
      "sometimes bisociates two unrelated knowledge domains, synthesises the "
      "chain through a local model speaking as one of its lens nodes, and scores "
      "the result for urgency. What follows is what cleared the bar.\n")
    A("**Voices:** {}.\n".format(", ".join(agents)))
    A("**Reading each dream's header.** *Urgency* is the engine's own "
      "keyword-weighted score against a rolling percentile of recent dreams — "
      "higher means more unusual, not more true. *Lane* is which memory it drew "
      "from: `knowledge` (research, mathematics, technical notes) or `shared` "
      "(the book corpus). *Mode* is `classic` for a single-domain chain, or "
      "`cross-domain` when two unrelated domains were deliberately seeded and "
      "interleaved.\n")
    A("Dreams are ordered by urgency, strongest first. The **synthesis** is the "
      "engine's own reasoning; the **seed** and **fragments** beneath it are the "
      "raw archive material it reasoned from, kept as provenance.\n")
    A("---\n")

    A("## Index\n")
    A("| # | Urgency | Voice | Lane | Date | Opening claim |")
    A("|---|---|---|---|---|---|")
    for i, d in enumerate(live, 1):
        when = "{:%d %b}".format(d["when"]) if d["when"] else "—"
        A("| {} | **{}** | {} | {} | {} | {} |".format(
            i, d["score"], d["agent"], d["lane"], when, opener(d["syn"])))
    A("\n---\n")

    for i, d in enumerate(live, 1):
        when = "{:%d %B %Y, %H:%M}".format(d["when"]) if d["when"] else "date unknown"
        A("## {}. {}\n".format(i, opener(d["syn"], 90)))
        urg = "**Urgency {}**".format(d["score"])
        if d["floor"]:
            urg += "/{}".format(d["floor"])
        A(" · ".join([urg,
                           "voice **{}**".format(d["agent"]),
                           "lane `{}`".format(d["lane"]),
                           "`{}`".format(d["mode"]),
                           when,
                           "DreamID `{}`".format(d["id"])]) + "\n")
        A("### Synthesis\n")
        A(d["syn"] + "\n")
        if d["seed"]:
            A("### Seed\n")
            A("> " + d["seed"] + "\n")
        if d["frags"]:
            A("### Fragments it chained ({})\n".format(len(d["frags"])))
            for fr in d["frags"]:
                A("- " + fr)
            A("")
        A("---\n")

    if empty:
        A("## Appendix — cycles with no synthesis\n")
        A("{} cycles completed but returned no synthesis text: a reasoning model "
          "can spend its whole token budget on the thinking phase and emit empty "
          "content. Their seeds survive, so they are listed for completeness and "
          "excluded from the index above.\n".format(len(empty)))
        for d in empty:
            when = "{:%d %b %Y}".format(d["when"]) if d["when"] else "—"
            A("- **{}** · urgency {} · {} · `{}` · {} — seed: {}".format(
                d["id"], d["score"], d["agent"], d["lane"], when, opener(d["seed"], 130)))
        A("")

    A("---\n")
    A("*Generated from {} ping records by the Awen Engine — "
      "github.com/OwainGlyndwr1400/awen-engine*\n".format(len(dreams)))

    out = ROOT / args.out
    out.write_text("\n".join(L), encoding="utf-8")

    lanes = {}
    for d in live:
        lanes[d["lane"]] = lanes.get(d["lane"], 0) + 1
    print("wrote {}  ({:.0f} KB)".format(out.name, out.stat().st_size / 1024))
    print("  dreams with synthesis  {}".format(len(live)))
    print("  without synthesis      {}".format(len(empty)))
    if live:
        print("  urgency range          {} .. {}".format(live[-1]["score"], live[0]["score"]))
    print("  voices                 {}".format(len(agents)))
    print("  lanes                  {}".format(lanes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
