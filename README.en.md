# WeiZhi 微知

A learning butler I built for myself. Every day it picks a few things worth reading and turns them into cards, then brings them back to me a few days later. Everything on a card can be traced back to the source, and the quality gets checked and repaired automatically. It runs on one 2C2G machine, for about ¥0.2 a day in model costs.

[中文](README.md) | English

![Python](https://img.shields.io/badge/Python-3.10-blue) ![License](https://img.shields.io/badge/License-MIT-green) ![PWA](https://img.shields.io/badge/PWA-ready-orange) ![SQLite](https://img.shields.io/badge/Storage-SQLite-lightgrey) ![Tests](https://img.shields.io/badge/tests-578%20passed-brightgreen)

![WeiZhi](docs/assets/readme/hero.png)

## Live demo

http://47.76.25.13/?key=WeiZhi-Demo-ReadOnly-4t8m

That key is read-only. You can browse real cards, review sessions, the discovery feed and the quality report, but creating, editing or marking things as learned will not take effect — the server returns 403 rather than the frontend hiding buttons. A line at the top of the page says so.

## The problem it solves

My old habit was bookmarking good articles. The list kept growing; I finished none of them.

The issue was never a lack of content. Nothing decided which piece I should read today, and nothing made what I did read stick. It just scattered.

So the project had one goal from the start: whoever opens this reads something worth reading, and comes away with something.

Almost every trade-off later came out of that sentence. Why build a candidate pool first instead of turning whatever the feed returned into a card. Why a quality repair may never rewrite a card that has already been published. And why, on a day when nothing is good enough, it produces nothing at all.

## What it looks like

One real flow of use.

| Home | Discovery |
|---|---|
| ![Home](docs/assets/readme/01-home.png) | ![Discovery](docs/assets/readme/03-discover.png) |
| Today's cards, plus any structured study plan in progress. | Not a push channel — somewhere you go when you feel like browsing. Each pick carries one line on why it was chosen. When nothing is worth picking, it is empty. |

| Reading card | Spaced review |
|---|---|
| ![Card](docs/assets/readme/02-card.png) | ![Review](docs/assets/readme/04-review.png) |
| The footer has the source, its tier and a link to the original; citation numbers in the body resolve back. | Due cards enter an SM-2 queue. Multiple choice is graded server-side, short answers by the model. |

| Stats | Quality inspection |
|---|---|
| ![Stats](docs/assets/readme/06-knowledge-map.png) | ![Inspection](docs/assets/readme/08-inspection.png) |
| "How many cards have never entered review" is invisible until you draw it. | Runs at 3:30 daily: score, regenerate the bad ones, and every repair can be reverted. |

## Four decisions

Four calls I made, each with what it gave up. These four are here because all of them can be checked: screenshots, numbers, rows in a database.

### Facts on a card have to trace back to the source

The first version handed a whole article to the model and asked for a card. It read well but you could not verify it. The model would add background it remembered, or turn "an executive said" into "the company announced."

So generation became two steps. First extract facts from the source text deterministically, with their positions; then write using only those facts, tagging each paragraph with citation numbers. Material that yields too few facts produces no card.

The cost is speed and some wasted material, and the model no longer gets to add background on its own. What I get: source, source tier and an original link at the foot of every card, 1,659 extracted facts in the database, and citation numbers that resolve.

<img src="docs/assets/readme/05-source.png" width="330" alt="Source and original link at the foot of a card">

### Quality should be measured, but repairs should not be silent

Model output varies and reading every card by hand does not scale. But "fully automatic self-healing" has one property I did not want: it would quietly rewrite content I had already read.

Now it inspects and scores daily, and only emits revision candidates. Accepting one is my call, each repair is logged and revertible.

I traded away some automation and gained a human decision step. The quality page reads "19 inspected · 68% pass · 4.17 average." I left the 68% in, because it gets caught and fixed by the system, and that says more than writing "quality is excellent."

<img src="docs/assets/readme/07-quality-trend.png" width="330" alt="Quality trend and revertible repair log">

### Staleness windows follow content lifespan, not one number

All eight sources used to share a single 7-day window. That treated "a 20-day-old essay" and "a 20-day-old news item" as the same thing: the essay became unreadable after a week, even though news goes stale faster.

Now the window is tiered by lifespan — 7 days for announcements, papers and media, 30 days for blogs and analyst newsletters, overridable per source.

The configuration went from one number to a table, which is more annoying. On the day I changed it, an August 24 essay (19 days old) produced a card under the 30-day window; the old rule would have rejected it outright.

### "Seen" is not "produced"

At fetch time the program fingerprint-marked every new item as seen, but only a handful were later picked for cards. The rest stayed permanently seen and were never considered again.

The symptom was strange: a middle block of four-month-old entries in one feed had never been processed, while both newer and older items counted as seen.

Now a fingerprint is written only when an item has a conclusion — it was picked and processed, or it aged past its window. Items that were not picked but are still inside the window stay in the pool for the next round. The cost is a bit more storage and repeat evaluation (the fingerprint store went from 300 to 3,000 entries).

The figure below is a real same-day comparison: the old rule dropped 145 items permanently, the new rule keeps 19 for the next round.

![Funnel](docs/assets/readme/funnel.png)

## It is actually running

Not a demo that starts up — a daily production instance. Figures are from 2026-09-12.

| | |
|---|---|
| First card | 2026-08-19 |
| Cards | 516 (frontier 180, plans 131, vocab 43, math 36, close reading 35, papers 30, fundamentals 23, skills 10) |
| Days with output | 25 |
| Sources | 8 |
| Extracted facts | 1,659, from 12 sources |
| Tests | 578, no model API calls |
| Deployment | One 2C2G Hong Kong server, single SQLite file, no external dependencies |

Call auditing started on 09-11: 75 calls, 194k input tokens, 42k output tokens, 3.6s average latency. At current DeepSeek list prices that is roughly ¥0.2 a day. The figure only covers calls that go through the unified provider interface; inspection and orchestrator calls are not wired in yet, so it is derived, not a bill.

## Structure

![Architecture](docs/assets/readme/architecture.png)

The directory follows the same four layers, and code is kept separate from data:

```
weizhi/
├── weizhi/                    # runtime package, split by responsibility
│   ├── core/                  #   foundations: data layer, model gateway, schema, paths
│   ├── produce/               #   ingest and card production: fetch, select, extract, write
│   ├── serve/                 #   reading and review: HTTP service, review flow, plans
│   │   └── web/               #   front-end assets (single-file PWA)
│   └── ops/                   #   quality and orchestration: inspection, agent, shadow
├── tools/                     # development and one-off scripts
├── deploy/                    # deploy scripts, systemd unit, nginx config
├── docs/                      # 21 product and design documents + index
│   └── assets/readme/         # images used by this README
└── tests/                     # 578 tests
```

`config.json` (it holds the keys), `weizhi.db`, `quality_reports/` and `backups/` are runtime data. They stay in the deployment root and never enter the repository. `weizhi/core/paths.py` separates the data directory from the web asset directory, so reorganising code does not touch data.

Data model and the finer design decisions are in [ARCHITECTURE.en.md](ARCHITECTURE.en.md) ([中文](ARCHITECTURE.md)).

### Scheduled jobs

| Time | Command | Purpose |
|---|---|---|
| 6:00 / 18:00 | `python -m weizhi.produce.pipeline` | Fetch 8 sources, build a candidate pool, filter twice, produce cards |
| 3:30 | `python -m weizhi.ops.daily_check` | Quality inspection and scoring, revision candidates |
| 3:35 | `python -m weizhi.ops.daily_agent` | Orchestrator decisions and notifications |
| 4:00 | `python -m weizhi.ops.v2_shadow` | Shadow pipeline, used to compare output |

## What you can check

578 tests covering fact extraction, writing gates, filter degradation, SM-2, SimHash dedup, response sealing (answers never reach the browser) and read-only permission boundaries. They run against a temporary SQLite database and never touch real data.

Every model call records provider, model, prompt version, input hash, latency, retries and errors; the input hash doubles as an idempotency cache key.

Of the 21 documents under `docs/` (plus an [index](docs/README.md)), one is dedicated to [what I deliberately cut](docs/范围决策-保留降级合并删除.md) and one to [how the repo and production stay in sync](docs/部署守则-仓库与线上同步.md) (both in Chinese).

The owner key and the read-only demo key are separate, and the demo role is refused every write operation on the server.

## Known shortcomings

- **The product's own success criterion is not instrumented.** I can prove a card was read; I cannot prove the reading changed a judgement. This is the biggest gap — without it, every quality metric measures output, not effect.
- **Call audit coverage is incomplete**, so the cost figure is derived (see above).
- **Ranking can squeeze depth sources when the pool is small.** Items are sorted by source tier before truncation, so on a day when fast sources fill the cap, blog items are cut systematically. Known, not fixed yet.
- **Single-user by design.** No multi-tenancy, no accounts; showing others means one read-only key.
- **The frontend is a single-file vanilla-JS page** with no build step, held together by render tests and discipline.

## Running it locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config.example.json config.json    # fill in DeepSeek API key, access_token, demo_token
python -m weizhi.serve.reader         # open http://localhost:8000/?key=<your token>

python -m weizhi.produce.pipeline --limit 2      # fetch once and produce cards
python -m weizhi.produce.pipeline --fetch-only   # inspect the pool only, no cards, no fingerprints
python -m weizhi.ops.daily_check --dry-run --limit 3   # preview the quality inspection
python -m weizhi.ops.daily_agent --dry-run       # preview orchestrator decisions
```

Tests:

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q     # 578 tests, ~45s, no model API calls
```

Scripts under `tools/` are run as `python -m tools.<name>` (they rely on the repo root being on `sys.path`).

## Security

`config.json` (API key and tokens) is excluded by `.gitignore`, along with its backups `config.json.bak*`; only `config.example.json` ships in the repo.

The service listens on `127.0.0.1` only and is reverse-proxied by nginx, with visitor IPs arriving via `X-Forwarded-For`. nginx deliberately does not serve static files from a directory root — that would give "where the file lives" two sources, and reorganising the code tree then breaks `sw.js` silently.

The read-only demo key is safe to publish; the owner key is not. To rotate, edit `config.json` and restart the service.

## License

MIT
