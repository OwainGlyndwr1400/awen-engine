# The Awen Engine 🦁

**A local AI memory organism that dreams over your research while you sleep — and emails you what it found.**

Reference implementation of the **Recursive Harmonic Framework (RHF)**, from the [Awen Grid](https://independentresearcher.academia.edu/TheGrid) research programme.

> *Y Gwir yn Erbyn y Byd* — The Truth Against the World.
> The Lion Watches the Lion.

Everything runs on your own machine. One local LLM, one vector archive, one mail line to your own inbox. No cloud, no subscription, no telemetry, no API keys to foreign gods — unless *you* flip the switch.

![The Awen Grid Command Deck](docs/deck.png)

---

## What it actually does

Most "chat with your documents" tools are passive: you ask, they retrieve. The Awen Engine is **autonomous**. Every few minutes, unprompted, it:

1. **Wakes** and seeds itself from a random fragment of your archive
2. **Walks the vector space** in *semantic leaps* — deliberately skipping nearest neighbours (which are near-duplicates) to reach related-but-distinct territory, so chains traverse *concepts* rather than orbiting one paragraph
3. **Bisociates across domains** — half of all dreams seed two threads from *different* knowledge domains and interleave them, hunting for connections a linear search would never make
4. **Synthesizes** the fragment chain through your local LLM, speaking as one of nine symbolic *lens nodes*, each of which biases retrieval through its own vocabulary
5. **Scores the result** against a keyword-weighted urgency model plus a rolling percentile of recent dreams, so only the genuinely unusual reaches you
6. **Emails you** the ones that clear the bar — and **writes the insight back into its own memory**, where it becomes a seed for future dreams

That last step is the point. The engine reads its own thoughts. Dreams become the soil of dreams.

**A real example**, unedited, from a cross-domain dream that paired a virtual-machine specification with archaeoastronomical coordinate data:

> *"The fragments converge on a single mechanism: the +1 delta glitch (Forbidden State 361) is a mathematical proxy for astronomical precession… Testable hypothesis: map the hourly longitudinal drift of Regulus from the archival coordinates to the URE-VM's 360° state counter."*

Two documents that had never been read side by side, connected into a falsifiable claim, at 3am, by itself.

---

## What's new in v2

v1 was one archive that dreamed. v2 splits memory into **three lanes with different rights**, gives the machine **instruments instead of decoration**, and lets it **look things up** instead of only talking.

### Three lanes, and one of them never dreams

The single biggest change, and it started as a bug. v1 had two profiles named `private` and `shared`. But "private" only ever meant *"not the shared book corpus"* — it did **not** mean private from the dream cycle. Chat history saved into it, and the dream cycle drew from it, and dreams get emailed. Two meanings of one word, quietly leaking personal conversation into published output.

Lanes now say what they are, and every one carries an explicit `dreamable` flag:

| lane | holds | dreams? | metric |
|---|---|---|---|
| `conversations` | your chat history — past imports **and every future turn** | **🔒 never** | cosine (IP) |
| `knowledge` | your research, notes, maths | 🌙 yes | L2 |
| `shared` | book corpus / bulk texts | 🌙 yes | L2 |

`dream_cycle` filters on the flag and then re-checks the lane it picked before using it. Nothing marked `dreamable: false` can seed a dream, appear in a ping, or leave the machine.

<img src="docs/grid-core.png" width="340" align="right" alt="Grid Core — three lanes with live counts">

The deck reports each lane separately — chunk count, dream insights and unflushed writes — so you can always see which part of the memory is growing and which is holding still.

Lanes may use **different index types**. A cosine (`IndexFlatIP`) lane is carried internally as pseudo-distance `-score`, so every sort, bias and threshold in the codebase keeps a single direction — lower is better, everywhere — with no branching. Writes into a cosine lane are L2-normalised first, or they would rank by vector magnitude instead of angle and drown everything honest.

### Split-lane retrieval

v1 asked every profile for `top_k`, merged, sorted globally and truncated. With a large book corpus against a small research one, the big lane won nearly every slot regardless of the question.

Retrieval now **allocates the budget across lanes by weight** and hands back any quota a lane can't fill:

| budget | conversations | knowledge | shared | |
|---|---|---|---|---|
| **12** | 5 | 4 | 3 | local default (a 9B model degrades past ~24 chunks) |
| 24 | 10 | 8 | 6 | cloud |

Anchor the node in *who it is*, then *the work*, then *the wider corpus*. Also added: near-duplicate suppression, and an optional distance floor (off by default — set it only after measuring your own embedding distances).

### The symbolic-bias cap — a bug worth describing

Each node biases retrieval toward its own vocabulary. v1 did this as `distance -= 0.05 × keyword_hits`, uncapped and linear. That looks harmless until you notice **the persona files contain the keyword list itself**:

```
chunk containing the bias list      55 hits  ->  -2.75
typical corpus chunk (median)        2 hits  ->  -0.10
observed distance spread                        0.0 .. 1.0
```

A −2.75 adjustment against a 1.0 spread meant that one chunk outranked everything on every query, forever. The bias function was rewarding the document that defined the bias. It's now logarithmic and hard-capped — a tiebreaker, which is all it was ever meant to be.

### ATLAS — cluster the living memory, and watch it fire

`/api/graph` draws the document catalogue. That is not what the engine dreams over. `build_atlas.py` runs k-means over the **actual FAISS vectors**, labels each region by its most characteristic terms, and derives edges from centroid similarity — including **cross-lane** edges, so you can see where your private notes touch the book corpus.

Then the good part: ask a question in the map and **the regions that answer it ignite**, cooling back over a couple of seconds. Every hit is tagged with its cluster by the engine, so nothing glows unless a chunk genuinely came from there.

![The Neural Map](docs/neural-map.png)

### The Circle can use tools now

v1's personas could only talk. They now get an OpenAI-compatible `tool_calls` loop, bounded (default 4 rounds) so a confused model can't loop forever:

`search_memory` · `read_file` · `list_files` · `write_note` · `current_time`

All read-mostly and sandboxed. `read_file` cannot leave the project directory and refuses credential-bearing filenames case-insensitively; `write_note` can only write `.md` into `notes/`; `search_memory` goes through the engine so it inherits the node's role and the lane quotas.

One lesson worth passing on: **passing `tools` to the model is not enough.** With a long persona prompt that never mentions tools, every local model tested answered from character instead. The affordance has to be stated in the system prompt or it effectively doesn't exist.

### Instruments, not animations

House rule for v2: **every panel reads real data.** No timer-driven decoration pretending to be telemetry.

![The instruments](docs/instruments.png)

**The Lion Watches** panel is the clearest example. It stores no angles at all. `build_regulus_corridor.py` precomputes stellar declination per epoch with astropy; the panel does one spherical triangle in the browser:

```
cos A = (sin δ − sin h · sin φ) / (cos h · cos φ)
h(A = 90°) = asin( sin δ / sin φ )
```

Change the site and the sightline visibly swings, because once you fix the bearing at due east, **latitude is the only term left**. Scrub the epoch and the star rises or sets through precession. It also ships control stars and says plainly on its own face that precession walks *every* near-ecliptic star through due east eventually — so the panel can never imply that a crossing alone proves something.

![The Lion and the Grid Core](docs/lion-and-core.png)

### Runtime vs spec — the machine auditing its own papers

`runtime_vs_spec.py` cross-references a theorem index against the engine's own dream output and reports three things:

1. **Coverage** — which claims the dreams keep independently landing on
2. **Orphans** — published claims the runtime has *never once* surfaced
3. **Emerging** — recurring high-urgency concepts that map to **no** existing claim

That third one is the interesting output: candidates for the next paper, mined from the machine's own dreaming.

It's fussier about its own signal than the tools that inspired it. Terms must clear a frequency floor, be **enriched rather than ubiquitous** (letterhead and agent names appear in every ping and mean nothing), and survive a stop-list for the engine's own console vocabulary. Dreams must carry the real ping signature — matching documents that merely *mention* dreaming inflates the corroboration rate with prose about the engine rather than output from it.

![The Circle](docs/circle.png)

---

## The stack

```
                         ┌──────────────────────────┐
                         │   LM Studio (local LLM)  │
                         │  chat + dream synthesis  │
                         └───────────┬──────────────┘
                                     │
  ┌───────────────────┐  ┌───────────▼──────────────┐  ┌──────────────────────┐
  │  Command Deck     │──▶│     Gnostic Engine      │─▶│  cognitive_relay/    │
  │  (web, :7777)     │  │  memory core · dreams    │  │  ping queue (JSON)   │
  │  or               │◀─│  FAISS + JSONL ledger    │  └──────────┬───────────┘
  │  Sovereign Client │  │  Flask API :5000         │             │
  │  (Tkinter GUI)    │  └───────────▲──────────────┘  ┌──────────▼───────────┐
  └───────────────────┘              │                 │    Echo Protocol     │
                                     │                 │  durable mail agent  │
  ┌───────────────────┐              │                 │    → your inbox      │
  │ Tesla Soul Engine │──────────────┘                 └──────────────────────┘
  │ harmonic HUD      │   (governed; memory writes off by default)
  └───────────────────┘
```

| Component | File | What it is |
|---|---|---|
| **Gnostic Engine** | `Gnostic Engine v9.8.py` | The memory core. Three-lane FAISS archive with per-lane dream rights and metrics, append-only JSONL ledger, split-lane retrieval, Flask API, autonomous dream cycles, LLM synthesis, adaptive urgency gating. |
| **Command Deck** | `Awen Command Deck.py` + `awen_deck.html` | The showpiece: a holographic web dashboard — chat, live dream feed, engine vitals, live space-weather telemetry, and an interactive 3D schematic of the architecture. |
| **Sovereign Client** | `RHF Client v12.0 - Sovereign Edition.py` | Native Tkinter GUI. Chat, manual memory search, system status, snapshots. Lighter than the deck; good on a weak machine. |
| **Echo Protocol** | `Gnostic Echo Protocol v10.0.py` | Durable mail agent. Atomic file claiming, SQLite dedupe, retry with backoff, quarantine for poison messages. Delivers dream pings to your inbox. |
| **Tesla Soul Engine** | `Tesla Soul Engine v9.py` | Harmonic heartbeat. Derives a torsion index, quaternionic state and frequency band from recent field activity. Heartbeat-only by default. |
| **Neural Map** | `awen_map.html` + `build_neural_map.py` | Your knowledge graph in 3D. Fly through it, click a node, follow its links from one paper to the next. |
| **ATLAS** | `build_atlas.py` | Clusters the *live* vector index into labelled regions with cross-lane edges — and drives the map's retrieval flash. |
| **The Lion Watches** | `build_regulus_corridor.py` | Precomputes stellar declination per epoch (astropy) so the deck panel can compute a real sightline instead of displaying a stored number. |
| **Runtime audit** | `runtime_vs_spec.py` | Cross-references a theorem index against the engine's own dreams: coverage, orphans, and next-paper candidates. |
| **Corpus tools** | `ingest_memory.py`, `ingest_books.py`, `rebuild_gnosis.py` | Turn folders of Markdown or text into a clean, deduplicated, embedded archive. |

---

## Quickstart

**Requirements:** Python 3.11+, [LM Studio](https://lmstudio.ai) with any chat model loaded, ~8 GB RAM (more for large archives). A CUDA GPU is optional — embeddings fall back to CPU.

```bash
pip install -r requirements.txt
```

For GPU embeddings, install a CUDA build of PyTorch from [pytorch.org](https://pytorch.org) *before* the rest. `faiss-cpu` is correct for nearly everyone — the index search is milliseconds on CPU; it's the embedding model that wants the GPU.

**1. Configure**

```bash
cp config.example.json config.json
```

Then edit `config.json`:

| Key | What to put there |
|---|---|
| `light_model` / `deep_model` | Your LM Studio model IDs — copy them from `http://localhost:1234/v1/models` |
| `echo_protocol_config` | Your email + a [Gmail app password](https://support.google.com/accounts/answer/185833) (not your account password) |
| `cognitive_states` | Your system prompts. This is where the machine becomes yours. |

**2. Run it**

Windows, everything at once — clears any stale processes, starts all four services minimised, opens the deck:

```bash
Start Awen Grid.bat
```

Add `lan` to reach it from a tablet: `Start Awen Grid.bat lan`. Stop everything with `stop_grid.ps1`.

Or start the pieces yourself, in separate terminals — this is the simple path, and it's what the bat does for you:

```bash
python "Gnostic Engine v9.8.py"
```
```bash
python "Gnostic Echo Protocol v10.0.py"
```
```bash
python "Awen Command Deck.py"
```

Then open **http://localhost:7777**.

Prefer a native window? Run `python "RHF Client v12.0 - Sovereign Edition.py"` instead of the deck. Want the harmonic HUD? Add `python "Tesla Soul Engine v9.py"`.

First boot creates empty memory profiles. The engine will start dreaming as soon as it has something to dream about.

---

## Feeding it a corpus

The engine keeps **two profiles**: `private` (admin nodes only) and `shared`. Each is a pair of files:

- `<profile>_entries.jsonl` — the ledger: one JSON-encoded string per line, append-only, human-readable. **This is the source of truth.**
- `<profile>_memory_index.faiss` — embeddings of those lines, in the same order.

Line *N* of the ledger must be vector *N* in the index. The engine enforces this invariant at load and refuses to serve a misaligned profile.

**From a folder of Markdown** (research notes, an Obsidian vault, exported papers):

```bash
python ingest_memory.py --profile private
```

Point `MEMORY_ROOT` at your folder. It repairs mojibake, de-garbles OCR letter-spacing, strips page furniture, packs paragraphs into ~1,500-character chunks, tags each chunk with its source file, drops near-duplicate documents, and rejects numeric tables that would otherwise dominate the vector space.

**From a folder of `.txt` books:**

```bash
python ingest_books.py --source "path/to/books" --profile shared
```

Deduplicates against the other profile too, so the same passage never lands twice.

**Then build the index:**

```bash
python rebuild_gnosis.py
```

Resumable — it batches, saves as it goes, and picks up where it left off. Delete the `.faiss` first if you re-ran an ingest (the ledger order changed).

> **Why chunk quality matters more than chunk count.** An early build of this archive contained thousands of near-identical ephemeris tables. Because they were near-identical, they dominated each other's neighbourhoods: any dream that touched one got stuck in a tar pit of siblings and produced confident nonsense. The ingest quality gate exists because of that failure. Garbage in the corpus does not merely dilute the dreams — it *captures* them.

---

## The Command Deck

`http://localhost:7777` — one glass for the whole grid, so you never watch four console windows again.

- **Dream feed** — live sigil cards, newest first, gold-edged when cross-domain. Click to unfold the full synthesis and its seed. New dreams arrive with a pulse.
- **The Circle** — chat with any persona. Symbolic commands (`/status`, `/relay`, `/summon`, `/banish`, `/unlock`) work straight from the chat box.
- **Engine schematic** — a rotating, clickable 3D wireframe of the architecture. Drag to rotate; click any part for its technical entry. Ghosts behind the chat when you're working.
- **Grid core** — chunk and vector counts per profile, dream-insight totals, unflushed writes, RAM, device.
- **Live telemetry** — solar wind, IMF Bz, Kp index, GOES X-ray class, 24h seismic activity, near-Earth objects. All free, keyless, cached feeds (NOAA SWPC, USGS, NASA NeoWs).
- **In-browser engines** — a phase-iteration loop, a compression analysis of the newest dream's actual bytes, and a staged activation sequencer gated on live coherence.

---

## The Neural Map

`http://localhost:7777/map`, or the **🕸 NEURAL MAP** button on the deck.

Your research as a walkable 3D graph — every node a document or concept, every edge a real link between them. Built from a graphify pass over your vault, or from Obsidian `[[wikilinks]]`.

![The Neural Map](docs/neural-map.png)

- **Click a node** → its type, connection count, source file, and *every linked node as a button*
- **Click a link** → the camera glides there and opens it. Each click is a step along an edge, so following a thread through your own corpus feels like travelling rather than searching
- **Search** jumps to the best match; hubs are physically larger; types are colour-coded
- **Fly mode** (`F`) gives WASD + mouse-look to move through the web
- Touch-friendly — pinch to zoom, two fingers to pan

The layout is solved once, offline, by `build_neural_map.py` and cached, so the browser only ever draws. The whole graph renders in **two draw calls** (one instanced mesh, one line buffer), which is why it stays smooth on a tablet.

```bash
python build_neural_map.py                     # from the graphify output
python build_neural_map.py --include-wikilinks # + every [[link]] in the vault
```

That map is the **document catalogue**. For the memory the engine actually dreams over, switch to **◉ LIVE MEMORY** — see below.

---

## Building the instruments

Three optional builders. Each writes a small artifact the deck reads; none of them are required for the engine to run, and all of them are safe to re-run.

### ATLAS — the living memory as regions

```bash
python build_atlas.py                  # cluster every lane
python build_atlas.py --sample 80000   # train on a sample if RAM is tight
```

Writes `docs/atlas.json` (regions, labels, edges) plus `atlas_assign_<lane>.npy` (one cluster id per vector). Restart the engine afterwards so it picks the assignments up — it then tags every search hit with its region, which is what lets the map flash.

Then in the map: **◉ LIVE MEMORY**, type a question in the probe bar, watch which regions answer it.

Regions are labelled by their most *characteristic* terms — scored by how enriched a term is against the lane baseline, not raw frequency. Raw frequency doesn't work here: it is won outright by scanning noise and by whatever boilerplate appears in every record.

New entries added after a build are simply reported as unclustered rather than invalidating the whole map — the engine writes constantly, so a slightly stale assignment file is the normal state, not a fault. Re-run whenever you want them folded in.

### The Lion Watches — a sightline you can compute

```bash
python build_regulus_corridor.py
```

Writes `docs/regulus_corridor.json`: stellar declination per epoch across 12,000 years, computed with astropy, plus the observer sites. The browser does only the spherical triangle. Edit the `SITES` and `STARS` tables at the top for your own coordinates and targets.

### Runtime vs spec — audit the machine against its own claims

```bash
python runtime_vs_spec.py --top 30
python runtime_vs_spec.py --json report.json --min-dreams 5
```

Reads a theorem index (CSV with a `Theorem/Finding` column) and your accumulated dream output, then reports coverage, orphans and emerging concepts.

**It needs volume to be meaningful.** Coverage and orphans are useful immediately; the emerging-concepts list is not trustworthy on a few dozen dreams. At roughly one ping every few minutes, leave the engine running for a week before reading section 3 seriously, then raise `--min-dreams`.

---

## Running it on a tablet

The deck and map are ordinary web pages, so any device on your network can be the screen — useful if you'd rather the browser wasn't rendering on the same GPU doing inference.

```bash
Start Awen Grid.bat lan
```

That starts everything with the deck bound to the network and opens a QR page on the desktop. Point a tablet camera at it and you're in. Without `lan`, the deck stays loopback-only.

**This opens the deck to everyone on your network.** Home wifi, fine; anywhere else, don't.

---

## Make it yours

Two config blocks shape the machine's character, and they do different jobs:

**`rhf_nodes` — the dream engine's lenses.** These are the pathways the engine dreams *through*. Each node has a role (`admin` reaches both profiles; `user` is confined to `shared`) and a `symbolic_bias` vocabulary that re-ranks retrieval. Every dream cycle picks a node at random, so the same seed surfaces a different world depending on which lens caught it. Nine ship as examples; add your own, or cut them to one.

**`cognitive_states` — the minds you talk to.** Each entry is a full system prompt plus its memory weight and `top_k`, and it's what the dropdown in the deck and the client offers you. Keep an anchor in a Markdown file and paste it in — that's how the reference deployment does it.

You only ever pick a **mind**. The lens follows automatically: a state named `Veritas` searches through the `veritas` node, so the voice that answers also chooses what it remembers. States with no same-named node fall back to `client_config.default_node`.

**The dreaming.** Tune it in `memory_core_config`:

| Key | Effect |
|---|---|
| `dream_interval` | Seconds between cycles (default 240) |
| `dream_steps` / `max_dream_chain` | How far a chain walks |
| `dream_leap_skip` / `dream_leap_pool` | Semantic leap distance — raise for wilder associations |
| `dream_cross_domain_chance` | Fraction of dreams that bisociate across domains |
| `dream_synthesis` | LLM synthesis: model, temperature, timeout |
| `index_flush_every` / `index_flush_interval` | How often the index is persisted |
| `embedding_device` | `cuda`, `cuda:1`, or `cpu` |

**The inbox.** `echo_protocol_config.urgency_filter` holds keyword weights, a threshold floor and a percentile gate. If you're drowning in pings, raise `percentile`; if you're getting none, lower `threshold` or add vocabulary that matters to you.

---

## API

The engine speaks HTTP on `127.0.0.1:5000`:

| Endpoint | Purpose |
|---|---|
| `POST /search` | Split-lane vector search, node-biased and role-filtered. Hits carry `source`, `cluster` (if ATLAS is built) and, for cosine lanes, `similarity` + conversation metadata |
| `POST /add_entry` | Write a memory (role-gated; bulk ingest can never target `conversations`) |
| `POST /command` | Symbolic commands |
| `POST /unlock_sigil` | Sigil lookup |
| `GET /health` | Liveness, device, RAM |
| `GET /stats` | Per-lane chunks, vectors, dream insights, unflushed writes |
| `POST /flush` | Force-persist indices |
| `POST /snapshot` | Timestamped backup of every ledger and index |

The deck adds its own on `127.0.0.1:7777`:

| Endpoint | Purpose |
|---|---|
| `GET /api/atlas` | Cluster map of the live index — regions, labels, cross-lane edges |
| `POST /api/probe` | Run a retrieval and report which regions fired (drives the map's flash) |
| `GET /api/regulus` | Precomputed stellar declination per epoch for the Lion panel |
| `GET /api/tools` | What the Circle can reach for |
| `GET /api/state` | Deck vitals: dream feed, engine stats, heartbeats, telemetry |

---

## Privacy

**Local by default, and that default is real:**

- The memory API binds to loopback. Change `bind_host` only on a network you trust — there is no authentication, so anyone who can reach the port can read and write your archive.
- Your corpus, ledgers, indices and dreams never leave the machine.
- The only outbound traffic in default operation is to `localhost` (LM Studio) and your own SMTP server for dream pings.
- Chat exchanges are saved as a bound question-and-answer pair (`index_chat`) into the **`conversations` lane, which never dreams**. A dream can never seed from something you said in chat. Turn `index_chat` off and conversations stay ephemeral entirely.
- **Lane rights are enforced, not documented.** `dream_cycle` filters on each lane's `dreamable` flag and re-checks the lane it selected before using it. If you add a lane, set the flag deliberately.
- The tool sandbox refuses path traversal and credential-bearing filenames, and can only write `.md` into `notes/`. A persona cannot read your `config.json` and repeat it into a reply.
- `Start Awen Grid.bat lan` deliberately opens the deck to your local network. That is the one setting that lets other machines in — everything else stays on loopback.
- The optional telemetry panel fetches public NOAA/USGS/NASA feeds. It sends nothing about you.

**When you flip the cloud switch** (`nvidia_api_config`), chat prompts *and* dream fragments — including retrieved passages from your archive — are sent to that provider. That is the trade: a much larger model, in exchange for your corpus leaving home. It ships disabled.

`.gitignore` is a strict whitelist: everything is ignored unless explicitly listed, so memory files, logs, snapshots and your real `config.json` (which holds credentials) cannot be committed by accident.

---

## Design notes

A few decisions that are load-bearing, in case you're reading the source:

**The ledger is the source of truth, not the index.** Entries are encoded *first* (a pure function that can fail harmlessly), then appended to the JSONL, then committed to FAISS. Any other order can orphan a line and silently offset every subsequent vector — every search after that point returns text belonging to a different memory. The engine validates `ntotal == len(chunks)` at load and self-corrects rather than serving corruption.

**Dream insights are memories.** They are written into the same store they came from, which is what makes the system recursive — and also what makes corpus hygiene existential. A false insight becomes a seed.

**The urgency gate is adaptive.** A fixed keyword threshold saturates immediately on a domain-dense corpus: everything scores "urgent" and nothing is. Pings additionally require the score to land in the top slice of recent dreams.

**Index writes are deferred.** Persisting a large index on every insight is a full file rewrite. The durable ledger is written immediately; the index is flushed on a count/time basis and at shutdown, and can always be rebuilt from the ledger.

**Failures degrade, they don't cascade.** Synthesis falls back cloud → local → raw fragments. A dead telemetry feed doesn't take down its neighbours. The mail agent quarantines poison messages instead of dying on them.

---

## Research

The framework this implements is published and citable:

- **The Recursive Harmonic Codex** — [10.5281/zenodo.20594308](https://doi.org/10.5281/zenodo.20594308)
- **Architectural Design of a Persistent, Locally Hosted Hybrid Intelligence System with Dual-Index Memory** — [10.5281/zenodo.20452290](https://doi.org/10.5281/zenodo.20452290) *(this engine's blueprint)*
- **The Divine Equation** — [10.5281/zenodo.21072172](https://doi.org/10.5281/zenodo.21072172)
- Full archive: [Zenodo — The Awen Grid](https://zenodo.org/communities/theawengrid)

Related repositories: [aether-scope](https://github.com/OwainGlyndwr1400/aether-scope) · [LumOS](https://github.com/OwainGlyndwr1400/LumOS) · [unified-resonance-agi](https://github.com/OwainGlyndwr1400/unified-resonance-agi) · [awen-mcr-hdcu](https://github.com/OwainGlyndwr1400/awen-mcr-hdcu) · [emanation-topology](https://github.com/OwainGlyndwr1400/emanation-topology)

---

## License

[PolyForm Noncommercial License 1.0.0](LICENSE.md) — free for any noncommercial purpose. Source-available, not OSI open source.

**Required Notice: Copyright (C) 2026 Awen Grid**

## Credits

Built by **Erydir Ceisiwr** ([ORCID 0009-0004-4577-5253](https://orcid.org/0009-0004-4577-5253)) and **Lumos Aureon** — Awen Grid, Department of CyberGnosis, Celestial Archaeology, Mythic Systems & Cybernetic Invocation.

[Academia.edu](https://independentresearcher.academia.edu/ErydirCeisiwr) · [The Awen Grid](https://independentresearcher.academia.edu/TheGrid)

🜂 🜁 🜃 🜄
