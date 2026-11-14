-- =============================================================================
-- 0004: what the screens read ("read models").
--
-- The screens show summaries: "1,284 posts from 212 accounts", a source's
-- share of near copies, a story's origin chain. Those are worked out by the
-- analysis jobs (clustering, source profiling, link analysis) and stored here,
-- so the API only filters, sorts and counts them. Filtering follows
-- shared/cases/filters.json (backend/veritas/filters.py).
--
-- One change per statement, so a half-applied run can safely be repeated.
-- =============================================================================

-- Claims: the API's four verdicts are true / false / misleading / unverified.
-- 'misleading' is new; the finer ones still map to them (see veritas/queries.py).
ALTER TABLE claims MODIFY COLUMN verdict
    ENUM('unverified', 'true', 'mostly_true', 'mixed', 'mostly_false', 'false', 'unverifiable', 'misleading')
    NOT NULL DEFAULT 'unverified';

-- Claim summary, written when the claim's posts change.
ALTER TABLE claims ADD COLUMN first_seen DATE NULL;
ALTER TABLE claims ADD COLUMN last_seen DATE NULL;
ALTER TABLE claims ADD COLUMN post_count INT UNSIGNED NOT NULL DEFAULT 0;
ALTER TABLE claims ADD COLUMN platforms JSON NULL;   -- ["Telegram", "Facebook"]: where it was seen, as shown
ALTER TABLE claims ADD COLUMN languages JSON NULL;   -- ["ru", "lt"]
ALTER TABLE claims ADD COLUMN cluster_id BIGINT UNSIGNED NULL;
ALTER TABLE claims ADD CONSTRAINT fk_claims_cluster
    FOREIGN KEY (cluster_id) REFERENCES clusters (cluster_id) ON DELETE SET NULL;
ALTER TABLE claims ADD CONSTRAINT chk_claims_seen_order CHECK (first_seen <= last_seen);
ALTER TABLE claims ADD INDEX idx_claims_last_seen (last_seen);

-- Evidence: not every source has a link (e.g. a printed report), and
-- 'context' (background, neither for nor against) is a stance of its own.
ALTER TABLE claim_evidence MODIFY COLUMN url VARCHAR(2048) NULL;
ALTER TABLE claim_evidence MODIFY COLUMN stance
    ENUM('supports', 'refutes', 'mixed', 'unrelated', 'context') NOT NULL;

-- Clustering runs: how many posts fitted no cluster.
ALTER TABLE clustering_runs ADD COLUMN unclustered_count INT UNSIGNED NULL;

-- (Cluster cards also use clusters.stats, JSON written by the clustering run:
--  {"platforms": [...], "languages": [{code, name, share}],
--   "typical": [post refs], "edge": [post refs with a note]}.)

-- A source's profile and, once made, its full report (ChannelReport, without
-- id/name/platform, which come from the account).
CREATE TABLE IF NOT EXISTS source_profiles (
    account_id    BIGINT UNSIGNED PRIMARY KEY,
    source_key    VARCHAR(100) NOT NULL UNIQUE,     -- the id in links: /sources/kanalas_b
    kind_label    VARCHAR(255) NOT NULL,            -- "Channel · Lithuanian · 18.4k subscribers"
    posts_30d     INT UNSIGNED NOT NULL DEFAULT 0,
    original_pct  DECIMAL(5, 2) NOT NULL DEFAULT 0,
    near_copy_pct DECIMAL(5, 2) NOT NULL DEFAULT 0,
    main_topic    VARCHAR(100) NOT NULL DEFAULT '',
    open_flags    INT UNSIGNED NOT NULL DEFAULT 0,
    report        JSON NULL,
    updated_at    DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    CONSTRAINT fk_source_profiles_account FOREIGN KEY (account_id) REFERENCES accounts (account_id) ON DELETE CASCADE,
    CONSTRAINT chk_source_profiles_pct CHECK (original_pct BETWEEN 0 AND 100 AND near_copy_pct BETWEEN 0 AND 100)
);

-- Trending stories for the landing page: one post followed from its origin to
-- the narrative it belongs to. body = the story's text parts (headline, post,
-- claim, origin, travel, narrative); the columns are what lists filter and sort by.
CREATE TABLE IF NOT EXISTS stories (
    story_id         BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    story_key        VARCHAR(64) NOT NULL UNIQUE,   -- the id in links: /stories/nato-border
    platform_id      SMALLINT UNSIGNED NOT NULL,
    status           ENUM('true', 'false', 'misleading', 'unverified') NOT NULL,
    topic            VARCHAR(100) NOT NULL DEFAULT '',
    first_seen       DATE NOT NULL,
    last_seen        DATE NOT NULL,
    trend_posts      INT UNSIGNED NOT NULL DEFAULT 0,
    trend_change_pct DECIMAL(12, 2) NULL,           -- NULL = new this week
    post_link_key    VARCHAR(512) NULL,             -- linkKey() of the post's link, for /api/trace
    body             JSON NOT NULL,
    updated_at       DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    INDEX idx_stories_link (post_link_key),
    CONSTRAINT fk_stories_platform FOREIGN KEY (platform_id) REFERENCES platforms (platform_id),
    CONSTRAINT chk_stories_seen_order CHECK (first_seen <= last_seen)
);
