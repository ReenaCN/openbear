-- Recommendation module: business state only. Execution logs stay in the ordinary
-- conversation/runtime tables; every row here is owned by one Web owner.
CREATE TABLE IF NOT EXISTS rec_settings (
 owner_chat_id INTEGER PRIMARY KEY,
 revision INTEGER NOT NULL DEFAULT 1,
 config_json TEXT NOT NULL,
 folder_id TEXT NOT NULL DEFAULT '',
 updated_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rec_runs (
 run_id TEXT PRIMARY KEY,
 owner_chat_id INTEGER NOT NULL,
 kind TEXT NOT NULL,
 trigger TEXT NOT NULL,
 source_conversation_uuid TEXT NOT NULL DEFAULT '',
 conversation_uuid TEXT NOT NULL DEFAULT '',
 root_turn_uuid TEXT NOT NULL UNIQUE,
 status TEXT NOT NULL DEFAULT 'starting',
 error TEXT NOT NULL DEFAULT '',
 models_json TEXT NOT NULL DEFAULT '[]',
 batch_json TEXT NOT NULL DEFAULT '{}',
 cost_usd REAL NOT NULL DEFAULT 0,
 tokens INTEGER NOT NULL DEFAULT 0,
 model_calls INTEGER NOT NULL DEFAULT 0,
 cards INTEGER NOT NULL DEFAULT 0,
 local_date TEXT NOT NULL,
 started_at_ms INTEGER NOT NULL,
 finished_at_ms INTEGER,
 notification_state TEXT NOT NULL DEFAULT 'not_requested'
);
CREATE INDEX IF NOT EXISTS rec_runs_owner ON rec_runs(owner_chat_id,started_at_ms DESC);
CREATE INDEX IF NOT EXISTS rec_runs_conversation ON rec_runs(conversation_uuid,root_turn_uuid);
CREATE INDEX IF NOT EXISTS rec_runs_day ON rec_runs(owner_chat_id,local_date);
CREATE TABLE IF NOT EXISTS rec_cursors (
 owner_chat_id INTEGER NOT NULL,
 conversation_uuid TEXT NOT NULL,
 last_op_seq INTEGER NOT NULL DEFAULT 0,
 reference_revision INTEGER NOT NULL DEFAULT 0,
 digest TEXT NOT NULL DEFAULT '',
 last_user_at_ms INTEGER NOT NULL DEFAULT 0,
 analyzed_at_ms INTEGER NOT NULL DEFAULT 0,
 follow_seq INTEGER NOT NULL DEFAULT 0,
 follow_at_ms INTEGER NOT NULL DEFAULT 0,
 daily_page_json TEXT NOT NULL DEFAULT '',
 follow_page_json TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(owner_chat_id,conversation_uuid)
);
CREATE TABLE IF NOT EXISTS rec_profile (
 item_id TEXT PRIMARY KEY,
 owner_chat_id INTEGER NOT NULL,
 kind TEXT NOT NULL,
 topic TEXT NOT NULL,
 category TEXT NOT NULL DEFAULT '',
 detail TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT '',
 signal TEXT NOT NULL DEFAULT 'weak',
 location_json TEXT NOT NULL DEFAULT '{}',
 evidence_json TEXT NOT NULL DEFAULT '[]',
 first_seen_ms INTEGER NOT NULL DEFAULT 0,
 last_mentioned_ms INTEGER NOT NULL DEFAULT 0,
 user_edited INTEGER NOT NULL DEFAULT 0,
 removed_at_ms INTEGER,
 revision INTEGER NOT NULL DEFAULT 1,
 created_at_ms INTEGER NOT NULL,
 updated_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS rec_profile_owner ON rec_profile(owner_chat_id,kind,last_mentioned_ms DESC);
CREATE TABLE IF NOT EXISTS rec_cards (
 card_id TEXT PRIMARY KEY,
 owner_chat_id INTEGER NOT NULL,
 run_id TEXT NOT NULL,
 lane TEXT NOT NULL,
 category TEXT NOT NULL DEFAULT '',
 topic TEXT NOT NULL DEFAULT '',
 dedupe_key TEXT NOT NULL DEFAULT '',
 title TEXT NOT NULL,
 body TEXT NOT NULL,
 reason TEXT NOT NULL DEFAULT '',
 questions_json TEXT NOT NULL DEFAULT '[]',
 sources_json TEXT NOT NULL DEFAULT '[]',
 location_json TEXT NOT NULL DEFAULT '{}',
 material TEXT NOT NULL DEFAULT '',
 follow_json TEXT NOT NULL DEFAULT '{}',
 to_check_json TEXT NOT NULL DEFAULT '[]',
 review TEXT NOT NULL DEFAULT '',
 review_issue TEXT NOT NULL DEFAULT '',
 reviewed_at_ms INTEGER,
 origin_conversation_uuid TEXT NOT NULL DEFAULT '',
 priority INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT 'new',
 created_at_ms INTEGER NOT NULL,
 seen_at_ms INTEGER,
 removed_at_ms INTEGER
);
CREATE INDEX IF NOT EXISTS rec_cards_owner ON rec_cards(owner_chat_id,created_at_ms DESC);
CREATE INDEX IF NOT EXISTS rec_cards_dedupe ON rec_cards(owner_chat_id,dedupe_key);
CREATE TABLE IF NOT EXISTS rec_feedback (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 owner_chat_id INTEGER NOT NULL,
 card_id TEXT NOT NULL,
 action TEXT NOT NULL,
 detail TEXT NOT NULL DEFAULT '',
 conversation_uuid TEXT NOT NULL DEFAULT '',
 request_id TEXT NOT NULL DEFAULT '',
 created_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS rec_feedback_card ON rec_feedback(owner_chat_id,card_id,created_at_ms);
CREATE UNIQUE INDEX IF NOT EXISTS rec_feedback_request ON rec_feedback(owner_chat_id,request_id) WHERE request_id<>'';
CREATE TABLE IF NOT EXISTS rec_weights (
 owner_chat_id INTEGER NOT NULL,
 scope TEXT NOT NULL,
 name TEXT NOT NULL,
 weight REAL NOT NULL DEFAULT 0,
 updated_at_ms INTEGER NOT NULL,
 PRIMARY KEY(owner_chat_id,scope,name)
);
CREATE TABLE IF NOT EXISTS rec_follows (
 follow_id TEXT PRIMARY KEY,
 owner_chat_id INTEGER NOT NULL,
 card_id TEXT NOT NULL DEFAULT '',
 topic TEXT NOT NULL,
 kind TEXT NOT NULL,
 target TEXT NOT NULL,
 fingerprint TEXT NOT NULL DEFAULT '',
 last_value TEXT NOT NULL DEFAULT '',
 last_checked_ms INTEGER NOT NULL DEFAULT 0,
 last_changed_ms INTEGER NOT NULL DEFAULT 0,
 pending_change TEXT NOT NULL DEFAULT '',
 error TEXT NOT NULL DEFAULT '',
 active INTEGER NOT NULL DEFAULT 1,
 created_at_ms INTEGER NOT NULL,
 note TEXT NOT NULL DEFAULT '',
 last_update TEXT NOT NULL DEFAULT '',
 last_update_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS rec_follows_owner ON rec_follows(owner_chat_id,active);

-- Immutable spend survives deletion of model_calls / process conversations.
CREATE TABLE IF NOT EXISTS rec_usage (
 attempt_id TEXT PRIMARY KEY,
 run_id TEXT NOT NULL,
 owner_chat_id INTEGER NOT NULL,
 called_at_ms INTEGER NOT NULL,
 local_date TEXT NOT NULL,
 cost_usd REAL NOT NULL DEFAULT 0,
 tokens INTEGER NOT NULL DEFAULT 0,
 calls INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS rec_usage_day ON rec_usage(owner_chat_id,local_date);
CREATE INDEX IF NOT EXISTS rec_usage_run ON rec_usage(run_id);
CREATE TABLE IF NOT EXISTS rec_usage_migrations (version INTEGER PRIMARY KEY);
