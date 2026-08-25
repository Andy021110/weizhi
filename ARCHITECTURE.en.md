# WeiZhi · System Architecture

> Snapshot date: 2026-08-21 · In sync with the codebase

## 1. Overview

WeiZhi is a personal learning steward agent: a single-user learning system with an automated **perceive → think → act → feedback** loop. All services are built on the Python standard library (zero third-party frameworks), the frontend is a single-file vanilla JS page, data lives in a single SQLite file, and the whole system deploys on one HK lightweight VPS.

```
┌────────────────────────────── Perception ──────────────────────────┐
│  pipeline.py (6:00/18:00 cron)                                        │
│    RSS fetch → ETag conditional request → incremental (seen fp)      │
│    → SimHash cross-source dedup → DeepSeek card generation            │
└───────────────────────────────────────────────────────────────────┘
                               ▼ writes cards table
┌────────────────────────────── Action ──────────────────────────────┐
│  reader.py (systemd service, 127.0.0.1:8000)                         │
│    card CRUD / SM-2 scheduling / ranking(fresh×auth×interest)        │
│    classify / regen / rollback / notifications                        │
│  reader.html (PWA frontend)                                           │
│    list/detail/quiz/answer/review/tasks/calendar/stats/notifications  │
└───────────────────────────────────────────────────────────────────┘
                               ▼ daily snapshot
┌────────────────────────────── Feedback ────────────────────────────┐
│  daily_check.py (3:30 cron)                                          │
│    rule checks + DeepSeek 4-dimension scoring (acc/rel/depth/struct) │
│    → badcase list → auto-fix (score≤2.0, ≤3/round, ≤2/card)          │
│    → backup (backups/date/md5.json: old card + progress + new url)   │
└───────────────────────────────────────────────────────────────────┘
                               ▼ report + signals
┌────────────────────────────── Thought ─────────────────────────────┐
│  daily_agent.py (3:35 cron)                                          │
│    reads report + rule signals (pending/delay/streak/decline/stale)  │
│    → LLM decision (notifications / fix list / picks / weekly)        │
│    → whitelist validation → execute → fallback on LLM failure        │
└───────────────────────────────────────────────────────────────────┘
                               ▼ in-app notifications (bell + popup)
                             user (only judgment calls)
```

## 2. Data Model (SQLite: weizhi.db)

| Table | Purpose | Key fields |
|---|---|---|
| `cards` | knowledge cards | title/body/summary/think_*/quiz/review_quiz/template/timeliness/credibility/published/author/plan_id/plan_index/_meta/_gen_input |
| `plans` | study plans | title/template/status(active/paused/done)/total_cards/batch |
| `progress` | study records | date/card_source_url/done (calendar source) |
| `reviews` | review records | card_source_url/review_date/result/interval_before/interval_after (SM-2 trail) |
| `events` | behavior tracking | date/event/card_open·think_open·open_submit·calendar_open·search_use/source_url (profile & interest source) |
| `notifications` | steward notices | date/type/title/body/level(info/warn/action)/read |
| `user_state` | KV state | streak/last_active_date/seen_{source}(incremental fingerprints)/plan cursor |

## 3. Module Responsibilities

| Module | Boundary |
|---|---|
| `reader.py` | HTTP API + orchestration. Snapshots generation input into `_gen_input` for regen; regen preserves plan_id/plan_index |
| `db.py` | All SQL: schema, CRUD, stats (profile/interest/compliance/mastery), dedup (title normalization + SimHash) |
| `pipeline.py` | Fetch pipeline. `filter_fresh` incremental (seen fingerprints, keep 300); `_article_sim` summary-level SimHash (summary only, not title, to avoid false positives); first-party domain preferred over reprints |
| `prompts.py` | 6 templates + authority/freshness tiers + classification/disambiguation. All outputs include published/author/credibility/timeliness |
| `daily_check.py` | Inspection. Rule + AI dual channel; AI scoring temperature=0.2, skip on per-card failure; `_backup_count` caps regen per card |
| `daily_agent.py` | Orchestration. `rule_signals` (deterministic) + `think` (LLM) + `validate_decision` (whitelist) + `execute` + `fallback` |

## 4. Key Design Decisions

1. **Ranking formula**: `score = freshness × source authority × (1 + interest/10)`, read cards ×0.3 sink to bottom. Authority weights: official 1.2 / media 1.0 / professional blog 0.9 / self-media 0.7
2. **Auto-fix guardrails**: auto only when AI score ≤2.0 or rule-fixable; 2-3 scores stay for human judgment; backup files record `new_source_url` after successful regen (otherwise rollback cannot delete the new card — a bug fixed in v1)
3. **Classification + disambiguation in one call**: a single LLM call outputs both type and ambiguity directions; "AI auto" is default with manual override
4. **Freshness rules avoid false positives**: only cards with a real http source require `published`; topic-mode cards (no source) are exempt — fixed after v1 misreporting
5. **Notification consistency**: successfully fixed cards go to "auto-fixed", the rest to "pending"; LLM notification text passes type/level/length whitelist

## 5. Agent Evolution Path

| Stage | Status |
|---|---|
| AI pipeline + manual decisions | Superseded |
| Feedback loop (inspect → fix → rollback) | Shipped (M1-M3) |
| Steward agent v1 (LLM orchestration + in-app notifications + picks) | Shipped (current) |
| Full version: Web Push / adaptive pacing / goal layer | Planned (pending data validation) |

## 6. Cost Model (measured)

- Content generation (8-16 cards/day): ¥8-15/month (dominant cost, scales with volume)
- All intelligent layers (inspection/steward/picks/scoring): < ¥5/month
- Server (HK 2C2G): ¥40-60/month
- **Total ≈ ¥60-95/month**; intelligence is nearly free, cost is driven by daily content volume
