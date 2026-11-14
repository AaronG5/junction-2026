-- =============================================================================
-- 0005: "Analyze a link" runs as a job the screen polls (docs/backend-plan.md B6).
-- analysis_requests (init.sql) is the job record; it gets a public id, the
-- current step, the link's lookup key and the HTTP-style status of a failure.
-- One change per statement, so a half-applied run can safely be repeated.
-- =============================================================================

ALTER TABLE analysis_requests ADD COLUMN job_key CHAR(32) NULL;          -- random, shown in /api/analyze/{id}
ALTER TABLE analysis_requests ADD UNIQUE INDEX uq_analysis_requests_job (job_key);
ALTER TABLE analysis_requests ADD COLUMN link_key VARCHAR(512) NULL;     -- linkKey() of input_url
ALTER TABLE analysis_requests ADD INDEX idx_analysis_requests_link (link_key, status);
ALTER TABLE analysis_requests ADD COLUMN step TINYINT UNSIGNED NOT NULL DEFAULT 0;  -- 0-4 while running, 5 = done
ALTER TABLE analysis_requests ADD COLUMN error_status SMALLINT UNSIGNED NULL;      -- e.g. 503 when the AI stopped
ALTER TABLE analysis_requests ADD COLUMN started_at DATETIME(3) NULL;
