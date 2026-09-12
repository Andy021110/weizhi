# WeiZhi 微知

> A personal learning butler for people working in AI: every day it picks out the few pieces worth reading, turns them into study cards whose **every fact traces back to the original text**, and brings them back to you on a spaced-repetition schedule. It all runs on one 2C2G server, for about ¥0.2 a day in model costs.

[中文](README.md) | English

![Python](https://img.shields.io/badge/Python-3.10-blue) ![License](https://img.shields.io/badge/License-MIT-green) ![PWA](https://img.shields.io/badge/PWA-ready-orange) ![SQLite](https://img.shields.io/badge/Storage-SQLite-lightgrey) ![Tests](https://img.shields.io/badge/tests-578%20passed-brightgreen)

![WeiZhi](docs/assets/readme/hero.png)

## Live demo

**http://47.76.25.13/?key=WeiZhi-Demo-ReadOnly-4t8m**

Read-only key. You can browse real cards, review sessions, the discovery feed and the quality
report, but creating, editing or marking-as-learned will not take effect — the server returns
403 rather than the frontend hiding buttons. A "demo mode · read-only" notice appears at the top.

---

## What this is

A **single-user**, self-hosted learning system. Not a platform, no multi-tenancy — it serves one person.
It solves my own problem: I spend a fair amount of fragmented time reading about AI, and I neither
retain it nor can tell what was worth reading.

Four stages form a closed loop:

| Stage | What it does |
|---|---|
| **Ingest** | 8 curated sources (official blogs / arXiv / tech media / deep blogs / analyst newsletters), incremental fetch + cross-source dedup |
| **Select** | Build a candidate pool first, then filter in two passes: code handles staleness and duplicates, the model handles "is this worth reading" and explains why |
| **Generate** | Extract traceable facts first, then write from them; produces 6 card types (vocab / close reading / math / trivia / skill / code) |
| **Review & self-check** | SM-2 spaced repetition with quizzes and AI grading; a daily quality inspection that only produces *revision candidates*, all revertible |

## Why I built it

My previous approach: bookmark good articles. The bookmarks grew; I finished none of them.
The problem was never a lack of content — it was that **nothing decided which piece I should read today**,
and nothing made what I did read stick.

So the design goal is one sentence: **whoever opens this should read something worth reading,
and come away with something.**

It sounds like a slogan, but it decided almost every trade-off afterwards — why there is a candidate
pool instead of "whatever the feed gives us becomes a card", why quality repairs may never silently
rewrite a published card, and why producing nothing is the right outcome on a day with nothing good.

## What it looks like

Left to right, top to bottom — one real flow of use:

| Home: what to read today | Discovery: today's top 3 |
|---|---|
| ![Home](docs/assets/readme/01-home.png) | ![Discovery](docs/assets/readme/03-discover.png) |
| Opens straight to today's cards, plus any structured study plans in progress. | Not a push channel — a place you wander into. Each pick carries one line on why it was chosen; it is empty when nothing is good enough. |

| Reading card: traceable body text | Spaced review |
|---|---|
| ![Card](docs/assets/readme/02-card.png) | ![Review](docs/assets/readme/04-review.png) |
| The card footer carries **source, source tier and a link to the original**, and each paragraph maps back to it. | Due cards enter an SM-2 queue; multiple-choice is graded server-side, short answers are graded by the model. |

| Stats: mastery and knowledge map | Quality inspection: pass rate and repairs |
|---|---|
| ![Stats](docs/assets/readme/06-knowledge-map.png) | ![Inspection](docs/assets/readme/08-inspection.png) |
| "How many cards have never entered review" is only visible once you draw it. | Runs automatically at 3:30, scores every card, regenerates the bad ones — every repair is revertible. |

---

## Four decisions that matter

A learning product can look good for many reasons — a strong model, a simple problem, good tooling.
These are **the calls I made in this project**, each with what it gave up and how it can be checked today.
They are here because "the product seems decent" and "there were clear judgements behind it" are different things.

### 1. Every fact in a card must trace back to the source

**Problem**: the earliest version fed a whole article to the model and asked for a card.
It read well, but you could not verify it — the model might add background it "remembered",
or turn "an executive said" into "the company announced".

**Decision**: split generation in two — first extract facts from the source text *deterministically*
(with positions), then write using only those facts, tagging each paragraph with citation numbers.
Material that yields too few facts produces no card at all.

**Gave up**: some prose flourish and some speed. The model cannot add background freely,
extraction costs an extra step, and some material is dropped for "not enough extractable facts".

**How to check**: card footers carry source, source tier and the original link; the database holds
**1,659** extracted facts from 12 sources; citation numbers in the body resolve back to the original.

### 2. Quality must be measured — but repairs may not be silent

**Problem**: model output varies, and reading every card by hand does not scale.
Meanwhile "fully automatic self-healing" sounds better but has a flaw: it would **quietly rewrite
content you have already read**.

**Decision**: inspect and score daily (objective rules + model scoring), but only emit
**revision candidates** for a human to accept; every repair is logged and revertible.

**Gave up**: the appeal of full automation, and some degree of autonomy — a human decision step remains.

**How to check**: the quality page shows "19 inspected · 68% pass · 4.17 average", the distribution of
issue types, the auto-repair success rate, and a revert entry for each repair.
**I did not hide the 68%** — a 68% that gets caught and fixed by the system says more than "quality is excellent".

### 3. Staleness windows follow content lifespan, not one global number

**Problem**: all 8 sources shared a single 7-day window, which treated "a 20-day-old deep essay"
and "a 20-day-old news item" as the same thing — the essay became unreadable after a week,
even though news goes stale faster.

**Decision**: tier the window by **content lifespan**: 7 days for fast sources (official announcements /
papers / media), 30 days for depth sources (blogs / analyst newsletters), overridable per source.

**Gave up**: configuration simplicity — the window is now a table rather than one number.

**How to check**: runs print the window policy; on the day of the change, an August 24 essay
(19 days old) produced a card under the new 30-day window — the old rule would have rejected it.

### 4. "Seen" is not "produced"

**Problem**: fetching marked every new item as *seen*, but only a handful were later picked for cards.
The rest became **permanently seen and were never considered again**. The symptom was a
"seen set with holes": a middle block of four-month-old entries in a feed had never been processed,
while both newer and older items counted as seen.

**Decision**: a fingerprint is written only when **that item has a conclusion** — either it was
picked and processed, or it aged past its staleness window. Items that were not picked but are
still within the window stay in the pool for the next round.

**Gave up**: a little storage and some repeated evaluation (the fingerprint store grew from 300 to 3,000).

**How to check**: the figure below is a real same-day comparison — the old rule dropped 145 items
permanently; the new rule keeps 19 for the next round.

![Funnel](docs/assets/readme/funnel.png)

---

## It is actually running

Not a demo that starts up — an instance in daily production (as of 2026-09-12):

| Metric | Value |
|---|---|
| First card | 2026-08-19 |
| Cards | **516** (frontier 180 · plans 131 · vocab 43 · math 36 · close reading 35 · papers 30 · fundamentals 23 · skills 10) |
| Days with output | 25 |
| Sources | 8 (official / papers / media / deep blogs / analyst) |
| Extracted facts | 1,659, from 12 sources |
| Tests | **578 passed** (no model API calls) |
| Deployment | One 2C2G Hong Kong server, single SQLite file, no external dependencies |

**Model cost**: the call audit (wired up from 09-11) recorded 75 calls, 194k input tokens,
42k output tokens, 3.6s average latency. At current DeepSeek list prices that is about **¥0.2/day**.
Caveat: the audit only covers paths that go through the unified provider interface; the inspection
and orchestrator calls are not included yet, so this is a **derived figure, not a bill**.

## Architecture

![Architecture](docs/assets/readme/architecture.png)

Modules: `reader.py` (long-running HTTP service) · `pipeline.py` (ingest and card production) ·
`evidence.py` (fact extraction) · `card_writer.py` (constrained writing) · `daily_check.py` (quality inspection) ·
`daily_agent.py` (orchestration and notifications) · `db.py` (SQLite layer) ·
`providers.py` (single model gateway: schema validation / retries / audit).

Data model, design decisions and cost model: [ARCHITECTURE.en.md](ARCHITECTURE.en.md) ([中文](ARCHITECTURE.md)).

### Automation

| Time (cron) | Task | Purpose |
|---|---|---|
| 6:00 / 18:00 | `pipeline.py` | Fetch 8 sources → candidate pool → two-pass filter → produce cards |
| 3:30 | `daily_check.py` | Quality inspection → revision candidates (revertible) |
| 3:35 | `daily_agent.py` | Orchestrator → notifications (picks / decisions / weekly report) |
| 4:00 | `v2_shadow.py` | Shadow pipeline, used to compare the new flow's output |

## What makes it verifiable

- **Tests**: 578, covering fact extraction, writing gates, filter degradation, SM-2, SimHash dedup,
  response sealing (answers never reach the browser) and read-only permission boundaries.
  Isolated with a temporary SQLite database; real data is never touched.
- **Call audit**: every model call records provider / model / prompt version / input hash /
  latency / retries / errors. The input hash doubles as an idempotency cache key.
- **Decision trail**: 21 design documents under `docs/`, including one dedicated to
  **what was deliberately cut** ([范围决策](docs/范围决策-保留降级合并删除.md), in Chinese)
  and one on how the repo and production stay in sync.
- **Read/write separation**: owner key and read-only demo key are distinct; the demo role is
  refused every write operation on the server.

## Known shortcomings

Listed because these say more than "I'm happy with it":

- **The product's own success criterion is not instrumented.** I can prove a card was read;
  I cannot prove the reading changed a judgement. This is the biggest gap — without it, every
  quality metric measures **output**, not **effect**.
- **Call audit coverage is incomplete**, so the cost figure is derived rather than billed (see above).
- **Ranking can squeeze depth sources when the candidate pool is small.** Items are sorted by
  source tier before truncation, so on a day when fast sources fill the cap, blog items are cut
  systematically. Known, not fixed.
- **Single-user assumptions.** No multi-tenancy, no accounts; "showing others" is currently
  one read-only key.
- **The frontend is a single-file vanilla-JS page** with no build step or components;
  changes rely on render tests and discipline.

## Getting started

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config.example.json config.json    # fill in DeepSeek API key, access_token, demo_token
python reader.py                      # open http://localhost:8000/?key=<your token>

python pipeline.py --limit 2          # run one fetch + card production by hand
python pipeline.py --fetch-only       # inspect the candidate pool only (no cards, no fingerprints)
python daily_check.py --dry-run --limit 3   # preview quality inspection
python daily_agent.py --dry-run       # preview orchestrator decisions
```

Tests:

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q     # 578 tests, ~45s, no model API calls
```

## Layout

```
weizhi/
├── reader.py / reader.html   # long-running service + single-file PWA frontend
├── pipeline.py               # ingest: candidate pool + two-pass filter + card production
├── evidence.py               # deterministic extraction of traceable facts
├── card_writer.py            # evidence-constrained writing + writing gates
├── daily_check.py            # quality inspection and revision candidates
├── daily_agent.py            # orchestrator: notifications and decisions
├── db.py / providers.py      # SQLite layer / single model gateway
├── config.example.json       # config template (config.json is never committed)
├── deploy.sh / nginx.conf / weizhi-reader.service / DEPLOY.md
├── docs/                     # 21 product and design documents (Chinese)
│   └── assets/readme/        # screenshots and diagrams used by this README
└── tests/                    # 578 tests
```

## Security notes

- `config.json` (API keys, tokens) is excluded by `.gitignore`, as are its backups
  (`config.json.bak*`); only `config.example.json` ships in the public repo.
- The service listens on `127.0.0.1` only and is reverse-proxied by nginx;
  visitor IPs arrive via `X-Forwarded-For`.
- The read-only demo key is safe to publish; the owner key is not. To rotate, edit
  `config.json` and restart the service.

## License

MIT
