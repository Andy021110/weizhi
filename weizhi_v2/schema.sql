PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS v2_sources (
  id TEXT PRIMARY KEY,
  source_type TEXT NOT NULL CHECK(source_type IN ('article','pdf','paper','repository','note')),
  canonical_uri TEXT NOT NULL,
  title TEXT NOT NULL,
  author TEXT,
  published_at TEXT,
  snapshot_path TEXT,
  snapshot_sha256 TEXT,
  status TEXT NOT NULL CHECK(status IN ('pending','ready','failed','superseded')),
  created_at TEXT NOT NULL,
  UNIQUE(canonical_uri, snapshot_sha256)
);

CREATE TABLE IF NOT EXISTS v2_goals (
  id TEXT PRIMARY KEY,
  outcome TEXT NOT NULL,
  current_level TEXT NOT NULL,
  use_context TEXT NOT NULL,
  daily_minutes INTEGER NOT NULL CHECK(daily_minutes BETWEEN 60 AND 120),
  success_evidence_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('draft','active','paused','completed','archived')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS v2_milestones (
  id TEXT PRIMARY KEY,
  goal_id TEXT NOT NULL REFERENCES v2_goals(id),
  position INTEGER NOT NULL,
  outcome TEXT NOT NULL,
  prerequisites_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL CHECK(status IN ('planned','active','validated','skipped')),
  UNIQUE(goal_id, position)
);

CREATE TABLE IF NOT EXISTS v2_learning_packs (
  id TEXT PRIMARY KEY,
  goal_id TEXT REFERENCES v2_goals(id),
  milestone_id TEXT REFERENCES v2_milestones(id),
  title TEXT NOT NULL,
  pack_objective TEXT NOT NULL,
  estimated_minutes INTEGER NOT NULL CHECK(estimated_minutes BETWEEN 5 AND 30),
  status TEXT NOT NULL CHECK(status IN ('planned','generating','candidate','published','completed','rejected','archived')),
  generation_version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS v2_pack_sources (
  pack_id TEXT NOT NULL REFERENCES v2_learning_packs(id),
  source_id TEXT NOT NULL REFERENCES v2_sources(id),
  source_role TEXT NOT NULL CHECK(source_role IN ('primary','corroborating','context','user_material')),
  PRIMARY KEY(pack_id, source_id)
);

CREATE TABLE IF NOT EXISTS v2_evidence_claims (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES v2_sources(id),
  claim_text TEXT NOT NULL,
  evidence_text TEXT NOT NULL,
  locator TEXT,
  claim_type TEXT NOT NULL CHECK(claim_type IN ('fact','publisher_claim','opinion','inference')),
  verification_status TEXT NOT NULL CHECK(verification_status IN ('supported','failed','conflicted','unknown')),
  verification_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS v2_source_processing_runs (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES v2_sources(id),
  status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
  stage TEXT NOT NULL,
  detail_json TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS v2_card_specs (
  id TEXT PRIMARY KEY,
  pack_id TEXT NOT NULL REFERENCES v2_learning_packs(id),
  concept_id TEXT NOT NULL,
  position INTEGER NOT NULL CHECK(position BETWEEN 1 AND 3),
  learning_objective TEXT NOT NULL,
  minimum_recall TEXT NOT NULL,
  transfer_task TEXT,
  prerequisites_json TEXT NOT NULL DEFAULT '[]',
  durable INTEGER NOT NULL DEFAULT 1 CHECK(durable IN (0,1)),
  estimated_minutes INTEGER NOT NULL CHECK(estimated_minutes BETWEEN 5 AND 10),
  active_version_id TEXT,
  UNIQUE(pack_id, position),
  UNIQUE(pack_id, concept_id)
);

CREATE TABLE IF NOT EXISTS v2_card_versions (
  id TEXT PRIMARY KEY,
  card_spec_id TEXT NOT NULL REFERENCES v2_card_specs(id),
  version_number INTEGER NOT NULL,
  content_json TEXT NOT NULL,
  evidence_hash TEXT NOT NULL,
  generator_version TEXT NOT NULL,
  qa_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('candidate','active','rejected','superseded')),
  created_at TEXT NOT NULL,
  UNIQUE(card_spec_id, version_number)
);

CREATE TABLE IF NOT EXISTS v2_card_claims (
  card_version_id TEXT NOT NULL REFERENCES v2_card_versions(id),
  claim_id TEXT NOT NULL REFERENCES v2_evidence_claims(id),
  usage_role TEXT NOT NULL CHECK(usage_role IN ('body','answer','caption','boundary')),
  PRIMARY KEY(card_version_id, claim_id, usage_role)
);

CREATE TABLE IF NOT EXISTS v2_learning_states (
  concept_id TEXT PRIMARY KEY,
  goal_id TEXT REFERENCES v2_goals(id),
  first_learned_at TEXT,
  next_review_at TEXT,
  interval_days INTEGER NOT NULL DEFAULT 0,
  ease REAL NOT NULL DEFAULT 2.5,
  review_count INTEGER NOT NULL DEFAULT 0,
  last_result INTEGER,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS v2_assessments (
  id TEXT PRIMARY KEY,
  concept_id TEXT NOT NULL REFERENCES v2_learning_states(concept_id),
  card_version_id TEXT REFERENCES v2_card_versions(id),
  assessment_type TEXT NOT NULL CHECK(assessment_type IN ('diagnostic','immediate','delayed','transfer')),
  question_version TEXT NOT NULL,
  result REAL,
  answer_text TEXT,
  duration_seconds INTEGER,
  answered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS v2_legacy_links (
  legacy_source_url TEXT PRIMARY KEY,
  card_spec_id TEXT REFERENCES v2_card_specs(id),
  migration_status TEXT NOT NULL CHECK(migration_status IN ('readonly','migrated','ignored')),
  note TEXT
);

CREATE INDEX IF NOT EXISTS idx_v2_packs_goal_status ON v2_learning_packs(goal_id, status);
CREATE INDEX IF NOT EXISTS idx_v2_cards_pack_position ON v2_card_specs(pack_id, position);
CREATE INDEX IF NOT EXISTS idx_v2_reviews_due ON v2_learning_states(next_review_at);
CREATE INDEX IF NOT EXISTS idx_v2_claims_source ON v2_evidence_claims(source_id);
CREATE INDEX IF NOT EXISTS idx_v2_source_runs ON v2_source_processing_runs(source_id, started_at DESC);
