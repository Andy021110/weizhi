# WeiZhi

Personal AI learning steward for AI practitioners. Fetches, generates, recommends, reviews, and self-heals — a full learning loop that runs unattended.

![Python](https://img.shields.io/badge/Python-3.10-blue) ![License](https://img.shields.io/badge/License-MIT-green) ![PWA](https://img.shields.io/badge/PWA-ready-orange) ![SQLite](https://img.shields.io/badge/Storage-SQLite-lightgrey)

## Overview

WeiZhi is a self-hosted learning system that turns fragmented time into retained knowledge:

- **Ingest** — RSS aggregation from 8 curated sources (official blogs, arXiv, deep technical blogs)
- **Generate** — DeepSeek produces structured knowledge cards across 6 templates (vocabulary / reading / math / general knowledge / skills / code)
- **Recommend** — daily Top-3 picks with reasoning, ranked by freshness × source authority × personal interest
- **Review** — SM-2 spaced repetition with quizzes, thinking questions, and AI-graded short answers
- **Self-heal** — daily quality inspection (rule checks + AI scoring); bad cards are regenerated automatically with backup and rollback

The system runs as a ReAct-style pipeline: observe → think (LLM decides) → act → report. It stops for human input only when a decision requires judgment.

## Features

- Type auto-detection when creating study plans (no manual template selection)
- Authority & freshness system: 4 timeliness tiers (stable / evolving / fast / event) × 4 credibility levels (A-D), with expiry hints on cards
- Learning profile: interest topics, weak cards (≥2 consecutive mistakes), review accuracy — feeds back into ranking and daily picks
- Programmatic notifications (bell + popup): pending decisions, review backlog, streak warnings, weekly report
- Incremental fetching with ETag conditional requests, cross-source SimHash dedup (first-party preferred), retry with backoff
- PWA frontend (add-to-home-screen), single-file vanilla JS, zero build step
- Entire intelligent layer costs < ¥10/month (DeepSeek API); total deployment fits on a 2C2G HK VPS

## Architecture

```
┌───────────── Perception ─────────────┐
│  pipeline.py (cron 6:00/18:00)        │
│  fetch → dedup → generate cards       │
└──────────────────────────────────────┘
                   ▼
┌───────────── Action ─────────────────┐
│  reader.py (persistent service)       │
│  CRUD / SM-2 / ranking / profile      │
│  reader.html (PWA frontend)           │
└──────────────────────────────────────┘
                   ▼
┌───────────── Feedback ───────────────┐
│  daily_check.py (cron 3:30)           │
│  rules + AI scoring → auto-fix+backup │
└──────────────────────────────────────┘
                   ▼
┌───────────── Thought ────────────────┐
│  daily_agent.py (cron 3:35)           │
│  LLM decisions: notify / fix / pick   │
└──────────────────────────────────────┘
```

Modules: `reader.py` (HTTP API + business logic) · `pipeline.py` (fetch pipeline) · `daily_check.py` (quality inspection) · `daily_agent.py` (orchestration) · `db.py` (SQLite layer) · `prompts.py` (templates + classification)

See [ARCHITECTURE.md](ARCHITECTURE.md) for data model, design decisions, and cost model.

## Quick Start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config.example.json config.json    # add your DeepSeek API key
python reader.py                      # open http://localhost:8000/?key=<your token>

python pipeline.py --limit 2          # fetch one round of RSS and generate cards
python daily_check.py --dry-run --limit 3   # preview quality inspection
python daily_agent.py --dry-run       # preview steward decisions
```

## Automated Pipeline

| Time (cron) | Task | Purpose |
|---|---|---|
| 6:00 / 18:00 | `pipeline.py` | Fetch 8 sources → generate new cards |
| 3:30 | `daily_check.py` | Inspect & score → auto-fix bad cards with backup |
| 3:35 | `daily_agent.py` | Steward decisions → notifications (picks / pending / weekly report) |

## Repository Layout

```
content-pipeline/
├── reader.py            # main service (HTTP API + business logic)
├── reader.html          # PWA frontend, single file
├── prompts.py           # 6 templates + authority/freshness + classification
├── db.py                # SQLite data layer
├── pipeline.py          # RSS pipeline: incremental + dedup + ETag + retry
├── daily_check.py       # quality inspection & auto-fix
├── daily_agent.py       # ReAct orchestration layer
├── config.example.json  # config template (never commit config.json)
├── deploy.sh / nginx.conf / weizhi-reader.service / DEPLOY.md
└── docs/                # product & design documents (14 files)
```

## Security

- `config.json` (API key / access token) is excluded via `.gitignore`; use `config.example.json` as template
- Production: SSH key auth, fail2ban, service bound to 127.0.0.1 behind nginx

## License

MIT
