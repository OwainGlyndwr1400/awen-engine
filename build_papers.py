"""
build_papers.py — turn the Academia.edu profile dump into a papers index.

The deck is entirely instruments for flying the grid. This is the one surface
aimed at a reader rather than an operator: what has this programme actually
published, what does each paper claim, and where do I go to check it.

Input is a copy-paste of the Academia.edu profile, saved as Markdown. The record
shape is regular:

    <title>
    by <authors>
    Zenodo - ... - Awen Grid - Github, <year>
    <links><abstract run together on one line>
    DownloadEdit
    2,367 Views  Top 5%
    3 Bookmarks 1 Related papers View impact

Scrubbed on the way out, because this artifact is meant to be publishable:
  * the operator's live ngrok tunnel host, which appears on every venue line
  * Google Drive file links
Both identify or expose the operator's own machine and have no business in a
repo or on a page other people load.

View counts are a SNAPSHOT from the day the profile was copied, not live data.
The capture date is stamped into the output so the panel can say "as of" rather
than implying it is reading Academia.edu in real time.

    py -3.11 build_papers.py                       # auto-find the dump
    py -3.11 build_papers.py --source "path.md" --date 2026-08-17
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "docs" / "papers.json"

# --- scrubbing -----------------------------------------------------------
NGROK = re.compile(r"https?://[A-Za-z0-9\-]+\.ngrok(?:-free)?\.(?:io|app|dev)/?", re.I)
GDRIVE = re.compile(r"https?://(?:drive|docs)\.google\.com/\S+", re.I)

# --- extraction ----------------------------------------------------------
ZENODO = re.compile(r"zenodo\.org/records?/(\d+)", re.I)
DOI_TXT = re.compile(r"10\.5281/zenodo\.(\d+)", re.I)
GITHUB = re.compile(r"https?://github\.com/[\w.\-]+/[\w.\-]+", re.I)
VIEWS = re.compile(r"([\d,]+)\s*Views?(?:\s*(Top\s*[\d.]+%))?", re.I)
BOOKMARKS = re.compile(r"([\d,]+)\s*Bookmarks?", re.I)
RELATED = re.compile(r"([\d,]+)\s*Related\s*papers?", re.I)
MENTIONS = re.compile(r"([\d,]+)\s*Mentions?", re.I)
YEAR = re.compile(r"\b(19|20)\d{2}\b")
URL_ANY = re.compile(r"https?://\S+")

TERMINATOR = "downloadedit"
NOISE_LINE = re.compile(
    r"^(DownloadEdit|Download|Edit|View impact|more|less|Show more|"
    r"Papers?|Books?|Drafts?|Talks?|Teaching Documents?|Conference Presentations?)\s*$",
    re.I)


def scrub(s: str) -> str:
    s = NGROK.sub("", s)
    s = GDRIVE.sub("", s)
    return re.sub(r"\s{2,}", " ", s).strip(" -–—,;")


def num(s):
    try:
        return int(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse(text: str):
    lines = [l.rstrip() for l in text.splitlines()]
    papers, buf = [], []

    def flush(block, stats_lines):
        """block = title/authors/venue/abstract lines; stats = the tail."""
        block = [l for l in block if l.strip() and not NOISE_LINE.match(l.strip())]
        if not block:
            return
        # Title is the first line that is not a bare URL.
        ti = next((i for i, l in enumerate(block)
                   if not l.strip().lower().startswith("http")), None)
        if ti is None:
            return
        title = scrub(block[ti])
        if len(title) < 12 or title.lower().startswith("by "):
            return

        rest = block[ti + 1:]
        joined = "\n".join(rest)

        authors = ""
        for l in rest:
            if l.strip().lower().startswith("by "):
                authors = scrub(l.strip()[3:])
                break

        # Everything that is not the byline/venue/link line is abstract.
        abstract_parts = []
        for l in rest:
            s = l.strip()
            if not s or s.lower().startswith("by "):
                continue
            if re.match(r"^(zenodo|academia|github)\b", s, re.I) and len(s) < 200:
                continue
            body = URL_ANY.sub(" ", s).strip()
            body = re.sub(r"^(full archive( here)?( is just the paper)?|repository)\b",
                          "", body, flags=re.I).strip()
            if len(body) > 120:
                abstract_parts.append(body)
        abstract = scrub(re.sub(r"\s+", " ", " ".join(abstract_parts)))

        zen = ZENODO.search(joined) or DOI_TXT.search(joined)
        doi = f"10.5281/zenodo.{zen.group(1)}" if zen else None
        gh = GITHUB.search(joined)
        # The year lives at the END of the venue line ("... - Github, 2026").
        # Searching the whole block instead picks up any 20xx inside the
        # abstract — which produced papers dated 2028 and broke "newest" sort.
        year = None
        for l in rest:
            m = re.search(r",\s*((?:19|20)\d{2})\s*$", l.strip())
            if m:
                year = int(m.group(1))
                break
        if year is None:                      # fall back to the venue-ish line only
            for l in rest:
                if re.match(r"^(zenodo|academia|awen grid)\b", l.strip(), re.I):
                    m = re.search(r"\b((?:19|20)\d{2})\b", l)
                    if m:
                        year = int(m.group(1))
                    break
        now = datetime.now().year
        if year and not (1990 <= year <= now + 1):
            year = None                        # a future date is a parse error

        stats = " ".join(stats_lines)
        v = VIEWS.search(stats)
        papers.append({
            "title": title,
            "authors": authors,
            "year": year,
            "doi": doi,
            "github": gh.group(0).rstrip("/.") if gh else None,
            "abstract": abstract,
            "views": num(v.group(1)) if v else None,
            "badge": (v.group(2).strip() if v and v.group(2) else None),
            "bookmarks": num(BOOKMARKS.search(stats).group(1)) if BOOKMARKS.search(stats) else None,
            "related": num(RELATED.search(stats).group(1)) if RELATED.search(stats) else None,
            "mentions": num(MENTIONS.search(stats).group(1)) if MENTIONS.search(stats) else None,
        })

    i = 0
    while i < len(lines):
        if lines[i].strip().lower() == TERMINATOR:
            # the following 1-2 lines are the stats tail
            tail = []
            j = i + 1
            while j < len(lines) and j <= i + 2 and lines[j].strip() and \
                    re.search(r"Views?|Bookmarks?|Related|Mention", lines[j], re.I):
                tail.append(lines[j]); j += 1
            flush(buf, tail)
            buf = []
            i = j
            continue
        buf.append(lines[i])
        i += 1

    return papers


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the papers index from an Academia.edu dump")
    ap.add_argument("--source", default="")
    ap.add_argument("--date", default="", help="capture date of the profile copy (YYYY-MM-DD)")
    ap.add_argument("--profile-url",
                    default="https://independentresearcher.academia.edu/ErydirCeisiwr")
    ap.add_argument("--orcid", default="0009-0004-4577-5253",
                    help="ORCID iD to fill in DOIs Academia did not paste "
                         "(empty string to skip the lookup)")
    args = ap.parse_args()

    if args.source:
        src = Path(args.source)
    else:
        cands = sorted((ROOT / "Memory" / "research").glob("*CyberGnosis*.md"))
        if not cands:
            print("❌ No Academia dump found. Pass --source.")
            return 1
        src = cands[0]
    if not src.exists():
        print(f"❌ not found: {src}")
        return 1

    print(f"parsing {src.name}")
    papers = parse(src.read_text(encoding="utf-8", errors="replace"))
    if not papers:
        print("❌ parsed 0 papers — the profile layout may have changed.")
        return 1

    # --- enrich DOIs from ORCID --------------------------------------------
    # Academia entries only sometimes paste their Zenodo link, so the flagship
    # paper can end up with no DOI while a minor one has it. ORCID is the
    # authoritative list; match on normalised title and fill the gaps.
    if args.orcid:
        import urllib.request
        url = f"https://pub.orcid.org/v3.0/{args.orcid}/works"
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            data = json.loads(urllib.request.urlopen(req, timeout=60).read())
            lookup = {}
            for group in data.get("group", []):
                for s in group.get("work-summary", []):
                    t = (s.get("title") or {}).get("title", {}).get("value", "")
                    ids = ((s.get("external-ids") or {}).get("external-id") or [])
                    doi = next((i.get("external-id-value") for i in ids
                                if str(i.get("external-id-type", "")).lower() == "doi"), None)
                    if t and doi:
                        lookup[re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()] = doi
            filled = 0
            for p in papers:
                if p["doi"]:
                    continue
                k = re.sub(r"[^a-z0-9]+", " ", p["title"].lower()).strip()
                hit = lookup.get(k)
                if not hit:      # prefix match — Academia titles are often longer
                    for lk, lv in lookup.items():
                        if len(lk) > 25 and (lk.startswith(k[:40]) or k.startswith(lk[:40])):
                            hit = lv
                            break
                if hit:
                    p["doi"] = hit
                    filled += 1
            print(f"  ORCID enrichment: +{filled} DOIs from {len(lookup)} records")
        except Exception as e:
            print(f"  ⚠️ ORCID lookup skipped ({e})")

    # Drop obvious duplicates by normalised title, keeping the richest record.
    best = {}
    for p in papers:
        k = re.sub(r"[^a-z0-9]+", " ", p["title"].lower()).strip()
        cur = best.get(k)
        score = sum(bool(p[f]) for f in ("doi", "github", "abstract", "views"))
        if not cur or score > cur[0]:
            best[k] = (score, p)
    papers = [p for _, p in best.values()]
    papers.sort(key=lambda p: (-(p["year"] or 0), -(p["views"] or 0)))

    captured = args.date or datetime.now().strftime("%Y-%m-%d")
    payload = {
        "generated": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "captured": captured,
        "profile_url": args.profile_url,
        "count": len(papers),
        "totals": {
            "views": sum(p["views"] or 0 for p in papers),
            "with_doi": sum(1 for p in papers if p["doi"]),
            "with_code": sum(1 for p in papers if p["github"]),
        },
        "note": ("Bibliography copied from the Academia.edu profile on "
                 f"{captured}. View and bookmark counts are a snapshot from that "
                 "date, not live figures."),
        "papers": papers,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    # Refuse to ship anything that leaks the operator's own machine.
    raw = OUT.read_text(encoding="utf-8")
    leaks = NGROK.findall(raw) + GDRIVE.findall(raw)
    print(f"\n  papers        {len(papers)}")
    print(f"  with DOI      {payload['totals']['with_doi']}")
    print(f"  with code     {payload['totals']['with_code']}")
    print(f"  total views   {payload['totals']['views']:,}  (as of {captured})")
    print(f"  scrub check   {'LEAKS: ' + str(leaks) if leaks else 'clean — no ngrok / drive links'}")
    print(f"\nwrote {OUT}  ({OUT.stat().st_size/1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
