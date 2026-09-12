# WeiZhi 微知 · System Architecture

> Updated 2026-09-12 · Architecture snapshot kept in sync with the code

## 1. Overview

A single-user, self-hosted personal learning system. Python standard library only (no frameworks), a single-file vanilla-JS frontend, data in one SQLite file, running on a 2C2G lightweight Hong Kong server.

Five processes, none blocking another (reader is long-running, the rest are cron-triggered):

| Process | How it starts | Frequency |
|---|---|---|
| `produce/pipeline.py` | cron | 6:00 / 18:00 |
| `serve/reader.py` | systemd, long-running | listening on 127.0.0.1:8000 |
| `ops/daily_check.py` | cron | 3:30 |
| `ops/daily_agent.py` | cron | 3:35 |
| `ops/v2_shadow.py` | cron | 4:00 (`--limit 1`) |

**Code and data are kept apart.** `weizhi/` is the code package; `config.json`, `weizhi.db`, `quality_reports/` and `backups/` live in the deployment root. `weizhi/core/paths.py` exposes `data_dir()` and `web_dir()`, and every path reference goes through it. Reorganising the code tree therefore requires migrating no data.

### Data flow

```
Fetch (8 sources, ETag-conditional)
  → Candidate pool (no longer "whatever the feed returns becomes a card")
  → Code filter: staleness window + same-title dedup + sort by source tier + truncate
  → Model filter: one call picks what to read today, with a reason
  → Evidence extraction: deterministic, position-bearing facts (v2_sources / v2_claims)
  → Constrained writing: only located facts are fed in, each paragraph cited
  → Writing gates: length, structure, information density; failing cards are dropped
  → Bridged into v1 card rows (cards)
  → Reading / spaced review (SM-2) / event tracking
  → Daily inspection scoring → only revision candidates, a human accepts them (revertible)
```

## 2. Directory and layers

```
weizhi/
├── weizhi/
│   ├── core/        foundations
│   │   ├── db.py            all SQL, schema creation, incremental migration
│   │   ├── providers.py     single model gateway: schema validation / retries / call audit
│   │   ├── schema_v2.py     versioned structures and validation for v2 data
│   │   ├── prompts.py       every prompt template (6 card types, classify/disambiguate, inspection, agent)
│   │   ├── notifications.py notification policy: only three kinds may reach the user
│   │   └── paths.py         where code lives vs where data lives
│   ├── produce/     ingest and card production
│   │   ├── pipeline.py      main line: candidate pool / pretriage / rank / produce
│   │   ├── news.py          source tiers, staleness classes, event normalisation
│   │   ├── evidence.py      deterministic evidence extraction (v2_sources / v2_claims)
│   │   ├── card_writer.py   evidence-constrained writing + writing gates
│   │   ├── card_gates.py    card quality gate rules
│   │   ├── assessment.py    question generation and delayed-review loop
│   │   ├── visual.py        semantic figures (plan, validate, drop individually)
│   │   └── bridge_v1.py     v2 draft → v1 card (carries tier and credibility)
│   ├── serve/       reading and review
│   │   ├── reader.py        HTTP API and business logic (including read/write roles)
│   │   ├── review_flow.py   due → answer → grade → mastery update
│   │   ├── mastery.py       mastery and forgetting risk
│   │   ├── planner.py       minimal learning pack planning
│   │   ├── goalspec.py      goal diagnosis
│   │   └── web/             front-end assets (reader.html / manifest / sw.js / icons)
│   └── ops/         quality and orchestration
│       ├── daily_check.py   inspection: rules + model scoring → revision candidates
│       ├── daily_agent.py   orchestration: model decision → whitelist validation → notifications
│       ├── revisions.py     reviewing and applying revision candidates
│       └── v2_shadow.py     shadow pipeline, for comparing the new flow's output
├── tools/           development and one-off scripts (python -m tools.<name>)
├── deploy/          deploy.py / deploy.sh / systemd / nginx
├── docs/            product and design documents + index
└── tests/           578 tests
```

## 3. Two card-production paths

This is the key to understanding the system: **the entry point is v1, but production defaults to the evidence chain.**

- **Evidence path (default)**: `_run_pick` in `pipeline.py` collects candidates, filters twice, then for each selected article calls `evidence.ingest_source()` to extract facts, `card_writer.write_card_gated()` to write under constraint, and on passing the gates `bridge_v1` assembles a v1 card row, writing source tier into `credibility`.
- **Legacy path (fallback)**: `_run_legacy` hands a whole article to the model with no evidence constraint. Only reachable via `--legacy`, for emergencies when the evidence chain breaks.
- **Shadow pipeline**: `v2_shadow.py` produces one card daily at 4:00 into `v2_card_drafts`; it never reaches the user's feed. Its job is to give a like-for-like comparison of the two flows on the same material.

If a source yields too few facts (`MIN_CLAIMS_FOR_PACK = 2`), no card is produced. One fewer card that day is the intended outcome.

## 4. Data model (SQLite: weizhi.db, 18 tables)

**Reading side**

| Table | Purpose | Key columns |
|---|---|---|
| `cards` | main card table | title / body / summary / quiz / review_quiz / template / timeliness / credibility / published / plan_id / `_gen_input` |
| `plans` | study plans | title / template / status / total_cards / batch |
| `progress` | completion records | date / card_source_url / done |
| `reviews` | review history | card_source_url / review_date / result / interval_before / interval_after |
| `events` | behaviour tracking | date / event / source_url |
| `notifications` | agent notifications | date / type / title / body / level / read |
| `ledger_concepts` | reading ledger | norm_id / term / one_line / why_matters / depends_on / count / first_seen / last_seen |
| `user_state` | key-value state | study days, incremental fingerprints `seen_global`, cursors and switches |

**Evidence and production side**

| Table | Purpose | Key columns |
|---|---|---|
| `v2_sources` | fetched material | url / title / site / content_hash / snapshot_path / clean_text / `meta` (source id, tier, topics, published_at) |
| `v2_claims` | facts extracted from material | source_id / claim_idx / text / start / end / kind / usable |
| `v2_card_drafts` | v2 drafts | input_hash / objective / capability_gap / figures / assessment / status / payload / gate_report |
| `v2_model_calls` | call audit | task / provider / model / prompt_version / input_hash / latency_ms / tokens / cost / retries / error / cached |

**Learning and governance side**

| Table | Purpose | Key columns |
|---|---|---|
| `v2_goals` | goal specs | goal_key / version / raw_input / spec / status |
| `v2_learning_packs` | learning packs | goal_key / milestone_id / status / card_ids / entry_count / failed |
| `v2_mastery` | mastery | goal_key / concept / score / attempts / correct / interval_days / next_review_at |
| `v2_review_log` | review detail | goal_key / concept / question_idx / correct / confidence / error_type |
| `card_revisions` | revision candidates | source_url / status / reason / origin / payload / prev_payload / decided_at |
| `demo_access` | read-only visitor log | ts / ip / method / path / blocked / ua (capped at 2,000 rows) |

## 5. Design decisions

1. **Recommendation ranking**: `score = freshness × source authority × (1 + interest/10)`, with already-read cards multiplied by 0.3. Authority comes from `CRED_WEIGHT`: official 1.2, reputable media 1.0, professional body 1.0, professional blog 0.9, self-media 0.7.
2. **Auto-repair guardrails**: only cards scoring ≤2.0 or failing a fixable rule are regenerated automatically; 2–3 is left for a human. After a successful regeneration the backup file has `new_source_url` appended, otherwise a rollback cannot delete the new card (the first version got this wrong).
3. **Type detection merged with disambiguation**: one call returns both the type and the ambiguity direction, saving a request. "Auto" is selected by default and can be overridden.
4. **Staleness rules avoid false alarms**: only `fast` / `event` cards with a real http source must carry `published`; topic-mode cards without an original are exempt.
5. **Notification consistency**: successfully repaired cards go to "auto-repaired", only unrepaired ones go to "needs a decision". Notification copy passes a whitelist on type, level and length.
6. **Evidence constraint**: facts are extracted deterministically first, writing happens second, and each paragraph carries citations. Too few facts means no card. The cost is speed and wasted material; what it buys is verifiability.
7. **Candidate pool with two-pass filtering**: code handles what can be computed (staleness, duplicates, source tier); the model handles what cannot (is this worth reading, and why). A source's publishing rate no longer directly decides what gets read today.
8. **Staleness windows follow content lifespan**: in `LOOKBACK_HOURS_BY_TIER`, announcements / papers / media get 7 days, blogs and analyst newsletters 30 days; a single source can override with `lookback_hours`. Precedence: source > tier > global.
9. **"Seen" is not "produced"**: a fingerprint is written only when an item has a conclusion (it was picked and processed, or it aged past its window). Items not picked but still inside the window stay in the pool. Store capacity `SEEN_CAP = 3000`. The old implementation wrote on fetch, which silently discarded everything that was not picked.
10. **Repairs produce candidates only**: inspection may regenerate, but never silently rewrites a published card; every repair is logged and revertible.
11. **Read/write separation**: `access_token` is the owner, `demo_token` is read-only. The write lock sits at the single `do_POST` entry point rather than in each route — per-route checks fail open the day someone forgets one, and they fail silently. The single exception is `/api/verify`, the endpoint that exchanges a token for a role.

## 6. Cost model

Derived from call auditing on 2026-09-11 and 09-12:

| Item | Value |
|---|---|
| Model calls | 75 calls, 194k input tokens, 42k output tokens |
| Average latency | 3.6 s |
| Model cost | about ¥0.2/day, so roughly ¥6/month |
| Server (Hong Kong, 2 vCPU / 2 GB) | ¥40–60/month |

Caveat: the audit only covers calls that go through the `providers.py` gateway; inspection and agent calls are not wired in yet, so this is a derived figure rather than a bill. 09-12 also includes several manual test runs, so a normal day is likely lower. The earlier "¥8–15/month for content generation" figure predates the audit; the order of magnitude agrees.

## 7. Roadmap state

| Stage | Status |
|---|---|
| Starting point: AI pipeline with manual decisions | superseded |
| Feedback loop (inspect → repair → roll back) | shipped |
| Agent orchestration (model decisions + in-app notifications + picks) | shipped |
| Evidence-constrained production (v1 frontend unchanged, new inner loop) | shipped, now the default path |
| Read-only demo and read/write separation | shipped |
| Learning goals and mastery (`v2_goals` / `v2_mastery` / `v2_learning_packs`) | data layer in place, frontend not wired |
| Instrumenting the product's own success criterion (did a judgement change) | **not started** |

That last row is the biggest gap: every quality metric measures **output**, not **effect**. Two other known issues remain unfixed — with a small candidate pool the tier-first sort squeezes depth sources out; and audit coverage is incomplete, so cost can only be derived.
