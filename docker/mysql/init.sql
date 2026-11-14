-- =============================================================================
-- Database schema (DRAFT v0.2) -- see docs/data-architecture.md for the design.
--
-- Same schema on the central server and on self-hosted installs.
-- Conventions:
--   * All times are UTC, DATETIME(3).
--   * Surrogate keys are BIGINT UNSIGNED AUTO_INCREMENT and are local to one
--     database. Rows that must match across databases (central <-> local, and
--     MySQL <-> Qdrant) also carry a deterministic `uuid` BINARY(16):
--       accounts.uuid = uuid5(NS, '<platform>:account:<external_id>')
--       posts.uuid    = uuid5(NS, '<platform>:post:<external_id>')
--     posts.uuid is also the post's Qdrant point id.
--   * Hashes (sha256) are stored as BINARY(32).
--   * Idempotent: safe to run on an existing database (IF NOT EXISTS,
--     INSERT IGNORE for seed data).
-- =============================================================================

SET NAMES utf8mb4;
SET time_zone = '+00:00';

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     VARCHAR(32) PRIMARY KEY,
    applied_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
);

-- -----------------------------------------------------------------------------
-- Reference data
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS platforms (
    platform_id SMALLINT UNSIGNED PRIMARY KEY,
    code        VARCHAR(32)  NOT NULL UNIQUE,     -- 'telegram', 'x', 'web', ...
    name        VARCHAR(100) NOT NULL,
    base_url    VARCHAR(255)
);

-- Which embedding model produced the vectors in a Qdrant collection.
-- Changing the model means re-embedding everything into a new collection.
CREATE TABLE IF NOT EXISTS embedding_models (
    embedding_model_id SMALLINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    code               VARCHAR(100) NOT NULL UNIQUE,  -- 'embeddinggemma-300m'
    dimensions         SMALLINT UNSIGNED NOT NULL,
    document_prompt    VARCHAR(255) NOT NULL,         -- template for stored texts
    query_prompt       VARCHAR(255) NOT NULL,         -- template for search queries
    is_active          BOOLEAN NOT NULL DEFAULT FALSE,
    created_at         DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
);

-- -----------------------------------------------------------------------------
-- Who publishes: real-world actors and their platform accounts
-- -----------------------------------------------------------------------------

-- A real-world person or organization (journalist, outlet, ministry, ...).
-- One actor can own many accounts across platforms.
CREATE TABLE IF NOT EXISTS actors (
    actor_id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    actor_type        ENUM('person', 'organization', 'unknown') NOT NULL DEFAULT 'unknown',
    name              VARCHAR(255) NOT NULL,
    country_code      CHAR(2),
    state_affiliation CHAR(2),            -- country whose state controls/funds it, if known
    description       TEXT,
    created_at        DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at        DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
    INDEX idx_actors_name (name)
);

-- Anything that publishes on a platform: a user profile, a channel, a group,
-- a page, or a website (platform 'web', external_id = domain).
CREATE TABLE IF NOT EXISTS accounts (
    account_id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    uuid                BINARY(16) NOT NULL UNIQUE,
    platform_id         SMALLINT UNSIGNED NOT NULL,
    external_id         VARCHAR(255) NOT NULL,          -- platform's own id (stable, unlike handles)
    handle              VARCHAR(255),                   -- @name, can change
    display_name        VARCHAR(255),
    account_type        ENUM('user', 'channel', 'group', 'page', 'website', 'unknown') NOT NULL DEFAULT 'unknown',
    url                 VARCHAR(2048),
    description         TEXT,
    language            VARCHAR(16),                    -- dominant language (BCP 47)
    country_code        CHAR(2),
    is_verified         BOOLEAN,
    platform_created_at DATETIME(3),                    -- account age is a bot signal
    actor_id            BIGINT UNSIGNED,
    is_stub             BOOLEAN NOT NULL DEFAULT FALSE, -- known only by reference, not scraped yet
    origin              ENUM('local', 'central') NOT NULL DEFAULT 'local',
    first_seen_at       DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    last_scraped_at     DATETIME(3),
    deleted_at          DATETIME(3),                    -- soft delete (erasure requests, retention)
    UNIQUE KEY uq_accounts_platform_ext (platform_id, external_id),
    INDEX idx_accounts_handle (platform_id, handle),
    INDEX idx_accounts_actor (actor_id),
    CONSTRAINT fk_accounts_platform FOREIGN KEY (platform_id) REFERENCES platforms (platform_id),
    CONSTRAINT fk_accounts_actor FOREIGN KEY (actor_id) REFERENCES actors (actor_id) ON DELETE SET NULL
);

-- Follower/post counts over time (sudden jumps are a bot/purchase signal).
CREATE TABLE IF NOT EXISTS account_metrics (
    account_id  BIGINT UNSIGNED NOT NULL,
    captured_at DATETIME(3) NOT NULL,
    followers   BIGINT UNSIGNED,
    following   BIGINT UNSIGNED,
    post_count  BIGINT UNSIGNED,
    PRIMARY KEY (account_id, captured_at),
    CONSTRAINT fk_account_metrics_account FOREIGN KEY (account_id) REFERENCES accounts (account_id) ON DELETE CASCADE
);

-- Scores about an account from any analysis tool. `kind` is open-ended on
-- purpose (e.g. 'bot_likelihood', 'credibility', 'coordination') so new
-- tools don't need schema changes. Latest row per (account, kind) wins.
CREATE TABLE IF NOT EXISTS account_assessments (
    assessment_id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    account_id    BIGINT UNSIGNED NOT NULL,
    kind          VARCHAR(64) NOT NULL,
    score         DECIMAL(5, 4),                  -- 0..1
    label         VARCHAR(100),
    method        VARCHAR(100) NOT NULL,          -- tool name + version
    details       JSON,
    assessed_at   DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_account_assessments_latest (account_id, kind, assessed_at),
    CONSTRAINT fk_account_assessments_account FOREIGN KEY (account_id) REFERENCES accounts (account_id) ON DELETE CASCADE
);

-- -----------------------------------------------------------------------------
-- What is published: posts (incl. news articles, comments, forwards)
-- -----------------------------------------------------------------------------

-- external_id conventions (must be identical on every install, they feed uuid):
--   telegram: '<chat_id>/<message_id>'        e.g. '-1001234567890/4521'
--   facebook: '<owner_id>_<post_id>'          (Graph-style id of the post)
--   web:      sha256(canonical url), hex
CREATE TABLE IF NOT EXISTS posts (
    post_id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    uuid               BINARY(16) NOT NULL UNIQUE,            -- = Qdrant point id
    platform_id        SMALLINT UNSIGNED NOT NULL,
    -- Who wrote it vs. where it appeared. Same account for a channel/page
    -- post; different for a member's post in a Telegram/Facebook group or a
    -- comment in a channel's discussion group.
    account_id         BIGINT UNSIGNED,                       -- author; NULL if hidden/unknown
    container_account_id BIGINT UNSIGNED,                     -- channel / group / page it appeared in
    external_id        VARCHAR(255) NOT NULL,
    url                VARCHAR(2048),
    post_type          ENUM('post', 'article', 'comment', 'reply', 'repost', 'video', 'other') NOT NULL DEFAULT 'post',
    parent_post_id     BIGINT UNSIGNED,                       -- thread parent for comments/replies
    title              VARCHAR(1024),
    text               MEDIUMTEXT,
    language           VARCHAR(16),
    content_hash       BINARY(32),                            -- sha256 of normalized text: exact copies
    byline             VARCHAR(255),                          -- article author as printed
    author_actor_id    BIGINT UNSIGNED,                       -- resolved author (journalist)
    published_at       DATETIME(3),
    edited_at          DATETIME(3),                           -- content changed after publishing
    scraped_at         DATETIME(3),
    is_stub            BOOLEAN NOT NULL DEFAULT FALSE,        -- referenced (e.g. forwarded from) but not scraped
    removed_on_platform BOOLEAN NOT NULL DEFAULT FALSE,       -- deleted by its author/platform since scraping
    embedding_model_id SMALLINT UNSIGNED,                     -- set once the vector is in Qdrant
    embedded_at        DATETIME(3),
    origin             ENUM('local', 'central') NOT NULL DEFAULT 'local',
    deleted_at         DATETIME(3),
    UNIQUE KEY uq_posts_platform_ext (platform_id, external_id),
    INDEX idx_posts_account_time (account_id, published_at),
    INDEX idx_posts_container_time (container_account_id, published_at),
    INDEX idx_posts_published (published_at),
    INDEX idx_posts_content_hash (content_hash),
    INDEX idx_posts_parent (parent_post_id),
    INDEX idx_posts_not_embedded (embedded_at),
    FULLTEXT INDEX ftx_posts_text (title, text),
    CONSTRAINT fk_posts_platform FOREIGN KEY (platform_id) REFERENCES platforms (platform_id),
    CONSTRAINT fk_posts_account FOREIGN KEY (account_id) REFERENCES accounts (account_id) ON DELETE CASCADE,
    CONSTRAINT fk_posts_container FOREIGN KEY (container_account_id) REFERENCES accounts (account_id) ON DELETE CASCADE,
    CONSTRAINT fk_posts_parent FOREIGN KEY (parent_post_id) REFERENCES posts (post_id) ON DELETE SET NULL,
    CONSTRAINT fk_posts_author FOREIGN KEY (author_actor_id) REFERENCES actors (actor_id) ON DELETE SET NULL,
    CONSTRAINT fk_posts_embedding_model FOREIGN KEY (embedding_model_id) REFERENCES embedding_models (embedding_model_id)
);

-- Engagement over time (views/likes/shares curves reveal artificial boosting).
CREATE TABLE IF NOT EXISTS post_metrics (
    post_id     BIGINT UNSIGNED NOT NULL,
    captured_at DATETIME(3) NOT NULL,
    views       BIGINT UNSIGNED,
    likes       BIGINT UNSIGNED,
    shares      BIGINT UNSIGNED,                     -- Telegram forwards / Facebook shares
    comments    BIGINT UNSIGNED,
    reactions   JSON,                                -- per emoji/type, e.g. {"👍": 120, "😡": 45}
    PRIMARY KEY (post_id, captured_at),
    CONSTRAINT fk_post_metrics_post FOREIGN KEY (post_id) REFERENCES posts (post_id) ON DELETE CASCADE
);

-- Images/videos attached to posts. Perceptual hash finds the same picture
-- reused across unrelated accounts, even when re-compressed or resized.
CREATE TABLE IF NOT EXISTS media (
    media_id        BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    post_id         BIGINT UNSIGNED NOT NULL,
    media_type      ENUM('image', 'video', 'audio', 'document') NOT NULL,
    url             VARCHAR(2048),
    sha256          BINARY(32),
    perceptual_hash BIGINT UNSIGNED,                 -- 64-bit pHash (images, video keyframes)
    ocr_text        MEDIUMTEXT,                      -- text in images (vision model / OCR)
    transcript      MEDIUMTEXT,                      -- speech in audio/video
    created_at      DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_media_post (post_id),
    INDEX idx_media_sha (sha256),
    INDEX idx_media_phash (perceptual_hash),
    CONSTRAINT fk_media_post FOREIGN KEY (post_id) REFERENCES posts (post_id) ON DELETE CASCADE
);

-- Outbound links found in posts. When the linked page is scraped too,
-- resolved_post_id points at it, which connects the propagation graph.
CREATE TABLE IF NOT EXISTS urls (
    url_id           BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    url              VARCHAR(2048) NOT NULL,          -- canonical form (no tracking params)
    url_hash         BINARY(32) NOT NULL UNIQUE,
    domain           VARCHAR(255) NOT NULL,
    resolved_post_id BIGINT UNSIGNED,
    first_seen_at    DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_urls_domain (domain),
    CONSTRAINT fk_urls_post FOREIGN KEY (resolved_post_id) REFERENCES posts (post_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS post_urls (
    post_id BIGINT UNSIGNED NOT NULL,
    url_id  BIGINT UNSIGNED NOT NULL,
    PRIMARY KEY (post_id, url_id),
    INDEX idx_post_urls_url (url_id),
    CONSTRAINT fk_post_urls_post FOREIGN KEY (post_id) REFERENCES posts (post_id) ON DELETE CASCADE,
    CONSTRAINT fk_post_urls_url FOREIGN KEY (url_id) REFERENCES urls (url_id) ON DELETE CASCADE
);

-- -----------------------------------------------------------------------------
-- How content spreads: the propagation graph
-- -----------------------------------------------------------------------------

-- Directed edge from the earlier post (source) to the later one (target).
-- E.g. Russian outlet article -> Telegram channel A (links_to)
--      -> Telegram channel B (forward) -> TV show transcript (paraphrase).
CREATE TABLE IF NOT EXISTS post_relations (
    relation_id    BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    source_post_id BIGINT UNSIGNED NOT NULL,
    target_post_id BIGINT UNSIGNED NOT NULL,
    relation_type  ENUM('repost', 'forward', 'quote', 'reply', 'links_to',
                        'copy', 'translation', 'paraphrase') NOT NULL,
    detected_by    ENUM('platform', 'url', 'hash', 'embedding', 'llm', 'manual') NOT NULL,
    similarity     DECIMAL(5, 4),                   -- for embedding/llm detections
    detected_at    DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    UNIQUE KEY uq_post_relations (source_post_id, target_post_id, relation_type),
    INDEX idx_post_relations_target (target_post_id),
    CONSTRAINT fk_post_relations_source FOREIGN KEY (source_post_id) REFERENCES posts (post_id) ON DELETE CASCADE,
    CONSTRAINT fk_post_relations_target FOREIGN KEY (target_post_id) REFERENCES posts (post_id) ON DELETE CASCADE,
    CONSTRAINT chk_post_relations_not_self CHECK (source_post_id <> target_post_id)
);

-- -----------------------------------------------------------------------------
-- What is being said: clusters (automatic) and narratives (curated)
-- -----------------------------------------------------------------------------

-- Categories a post can fall into, e.g. "anti-NATO", "election
-- interference", "health misinformation". Tree via parent_id. A post is
-- matched two ways: cheaply by vector similarity to the category's
-- prototype (Qdrant `categories` collection, built from description +
-- example posts), then confirmed by Gemma for the close calls.
CREATE TABLE IF NOT EXISTS categories (
    category_id    INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    parent_id      INT UNSIGNED,
    code           VARCHAR(64) NOT NULL UNIQUE,
    name           VARCHAR(255) NOT NULL,
    description    TEXT,                            -- also given to Gemma as the definition
    example_texts  JSON,                            -- example posts used to build the prototype vector
    prototype_uuid BINARY(16),                      -- point id in the Qdrant `categories` collection
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    CONSTRAINT fk_categories_parent FOREIGN KEY (parent_id) REFERENCES categories (category_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS post_categories (
    post_id     BIGINT UNSIGNED NOT NULL,
    category_id INT UNSIGNED NOT NULL,
    method      ENUM('embedding', 'llm', 'manual') NOT NULL,
    score       DECIMAL(5, 4),                      -- similarity or model confidence, 0..1
    model       VARCHAR(100),                       -- e.g. 'gemma-4-E4B-it'
    created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (post_id, category_id, method),
    INDEX idx_post_categories_category (category_id, score),
    CONSTRAINT fk_post_categories_post FOREIGN KEY (post_id) REFERENCES posts (post_id) ON DELETE CASCADE,
    CONSTRAINT fk_post_categories_category FOREIGN KEY (category_id) REFERENCES categories (category_id) ON DELETE CASCADE
);

-- A long-lived storyline, e.g. "NATO troops provoke Belarus" (more specific
-- than a category). Curated by people or confirmed from Gemma suggestions;
-- clusters from different runs get linked to the same narrative so it can
-- be followed over time.
CREATE TABLE IF NOT EXISTS narratives (
    narrative_id  BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    category_id   INT UNSIGNED,
    name          VARCHAR(255) NOT NULL,
    description   TEXT,
    status        ENUM('active', 'dormant', 'archived') NOT NULL DEFAULT 'active',
    first_seen_at DATETIME(3),
    last_seen_at  DATETIME(3),
    created_at    DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    CONSTRAINT fk_narratives_category FOREIGN KEY (category_id) REFERENCES categories (category_id) ON DELETE SET NULL
);

-- One clustering pass (e.g. UMAP + HDBSCAN over one week of posts).
CREATE TABLE IF NOT EXISTS clustering_runs (
    run_id             BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    embedding_model_id SMALLINT UNSIGNED NOT NULL,
    algorithm          VARCHAR(64) NOT NULL,          -- 'umap+hdbscan'
    params             JSON,
    window_start       DATETIME(3),
    window_end         DATETIME(3),
    post_count         INT UNSIGNED,
    cluster_count      INT UNSIGNED,
    status             ENUM('running', 'done', 'failed') NOT NULL DEFAULT 'running',
    started_at         DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    finished_at        DATETIME(3),
    CONSTRAINT fk_clustering_runs_model FOREIGN KEY (embedding_model_id) REFERENCES embedding_models (embedding_model_id)
);

CREATE TABLE IF NOT EXISTS clusters (
    cluster_id      BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    run_id          BIGINT UNSIGNED NOT NULL,
    label           VARCHAR(255),                    -- short LLM label from representative posts
    summary         TEXT,
    size            INT UNSIGNED NOT NULL,
    stats           JSON,                            -- language mix, top accounts, top domains, ...
    centroid_uuid   BINARY(16),                      -- point id in the Qdrant `clusters` collection
    narrative_id    BIGINT UNSIGNED,
    created_at      DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_clusters_run (run_id),
    INDEX idx_clusters_narrative (narrative_id),
    CONSTRAINT fk_clusters_run FOREIGN KEY (run_id) REFERENCES clustering_runs (run_id) ON DELETE CASCADE,
    CONSTRAINT fk_clusters_narrative FOREIGN KEY (narrative_id) REFERENCES narratives (narrative_id) ON DELETE SET NULL
);

-- Cluster membership per run. cluster_id NULL = noise (fits no cluster).
-- rank_in_cluster 1 = most typical post (closest to the centroid);
-- the highest ranks are the edge cases.
CREATE TABLE IF NOT EXISTS post_clusters (
    post_id              BIGINT UNSIGNED NOT NULL,
    run_id               BIGINT UNSIGNED NOT NULL,
    cluster_id           BIGINT UNSIGNED,
    distance_to_centroid FLOAT,
    rank_in_cluster      INT UNSIGNED,
    PRIMARY KEY (post_id, run_id),
    INDEX idx_post_clusters_rank (cluster_id, rank_in_cluster),
    CONSTRAINT fk_post_clusters_post FOREIGN KEY (post_id) REFERENCES posts (post_id) ON DELETE CASCADE,
    CONSTRAINT fk_post_clusters_run FOREIGN KEY (run_id) REFERENCES clustering_runs (run_id) ON DELETE CASCADE,
    CONSTRAINT fk_post_clusters_cluster FOREIGN KEY (cluster_id) REFERENCES clusters (cluster_id) ON DELETE CASCADE
);

-- -----------------------------------------------------------------------------
-- Is it true: claims and fact-checks
-- -----------------------------------------------------------------------------

-- A checkable statement, deduplicated across posts and languages (via the
-- Qdrant `claims` collection), so each claim is fact-checked once.
CREATE TABLE IF NOT EXISTS claims (
    claim_id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    uuid                BINARY(16) NOT NULL UNIQUE,  -- Qdrant point id
    text                TEXT NOT NULL,
    language            VARCHAR(16),
    verdict             ENUM('unverified', 'true', 'mostly_true', 'mixed', 'mostly_false',
                             'false', 'unverifiable') NOT NULL DEFAULT 'unverified',
    verdict_confidence  DECIMAL(5, 4),
    verdict_explanation TEXT,
    verdict_method      VARCHAR(100),
    verdict_at          DATETIME(3),
    created_at          DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_claims_verdict (verdict)
);

CREATE TABLE IF NOT EXISTS post_claims (
    post_id      BIGINT UNSIGNED NOT NULL,
    claim_id     BIGINT UNSIGNED NOT NULL,
    extracted_by VARCHAR(100),
    PRIMARY KEY (post_id, claim_id),
    INDEX idx_post_claims_claim (claim_id),
    CONSTRAINT fk_post_claims_post FOREIGN KEY (post_id) REFERENCES posts (post_id) ON DELETE CASCADE,
    CONSTRAINT fk_post_claims_claim FOREIGN KEY (claim_id) REFERENCES claims (claim_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS claim_evidence (
    evidence_id  BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    claim_id     BIGINT UNSIGNED NOT NULL,
    url          VARCHAR(2048) NOT NULL,
    title        VARCHAR(1024),
    publisher    VARCHAR(255),
    stance       ENUM('supports', 'refutes', 'mixed', 'unrelated') NOT NULL,
    excerpt      TEXT,
    method       VARCHAR(100),                       -- 'web_search+llm', 'manual', ...
    retrieved_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_claim_evidence_claim (claim_id),
    CONSTRAINT fk_claim_evidence_claim FOREIGN KEY (claim_id) REFERENCES claims (claim_id) ON DELETE CASCADE
);

-- -----------------------------------------------------------------------------
-- Results of analysis tools on posts
-- -----------------------------------------------------------------------------

-- Generic on purpose: every analysis (tone, intent, manipulation techniques,
-- stance, ...) writes rows here, with its own details in JSON. Most rows
-- come from Gemma prompts; analyzer_version = the prompt's version, so
-- results from an old prompt can be found and re-run.
CREATE TABLE IF NOT EXISTS post_analyses (
    analysis_id      BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    post_id          BIGINT UNSIGNED NOT NULL,
    analyzer         VARCHAR(100) NOT NULL,          -- analysis name, e.g. 'tone'
    analyzer_version VARCHAR(50) NOT NULL,           -- prompt/tool version
    model            VARCHAR(100),                   -- e.g. 'gemma-4-E4B-it' (results differ per model)
    label            VARCHAR(100),
    score            DECIMAL(6, 4),
    confidence       DECIMAL(5, 4),
    details          JSON,
    created_at       DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_post_analyses_post (post_id, analyzer, created_at),
    INDEX idx_post_analyses_label (analyzer, label),
    CONSTRAINT fk_post_analyses_post FOREIGN KEY (post_id) REFERENCES posts (post_id) ON DELETE CASCADE
);

-- -----------------------------------------------------------------------------
-- Dashboard: users, analysis requests, flags, scraping
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    user_id       BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    email         VARCHAR(255) UNIQUE,
    display_name  VARCHAR(100) NOT NULL,
    role          ENUM('admin', 'analyst', 'viewer') NOT NULL DEFAULT 'viewer',
    password_hash VARCHAR(255),                      -- NULL for the self-hosted local user
    created_at    DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    last_login_at DATETIME(3)
);

-- "Analyze this URL" from the dashboard (later: the browser extension).
-- Doubles as the job record and as history of what each user looked at.
CREATE TABLE IF NOT EXISTS analysis_requests (
    request_id  BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    user_id     BIGINT UNSIGNED,
    input_url   VARCHAR(2048) NOT NULL,
    target_type ENUM('post', 'account', 'unknown') NOT NULL DEFAULT 'unknown',
    platform_id SMALLINT UNSIGNED,
    post_id     BIGINT UNSIGNED,
    account_id  BIGINT UNSIGNED,
    status      ENUM('queued', 'scraping', 'analyzing', 'done', 'failed') NOT NULL DEFAULT 'queued',
    result      JSON,                                -- the insights shown to the user (snapshot)
    error       TEXT,
    created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    finished_at DATETIME(3),
    INDEX idx_analysis_requests_user (user_id, created_at),
    INDEX idx_analysis_requests_status (status),
    CONSTRAINT fk_analysis_requests_user FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE SET NULL,
    CONSTRAINT fk_analysis_requests_platform FOREIGN KEY (platform_id) REFERENCES platforms (platform_id),
    CONSTRAINT fk_analysis_requests_post FOREIGN KEY (post_id) REFERENCES posts (post_id) ON DELETE SET NULL,
    CONSTRAINT fk_analysis_requests_account FOREIGN KEY (account_id) REFERENCES accounts (account_id) ON DELETE SET NULL
);

-- User reports. Exactly one of post_id / account_id is set.
-- (CASCADE is fine with the CHECK below; SET NULL would not be allowed.)
CREATE TABLE IF NOT EXISTS flags (
    flag_id     BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    user_id     BIGINT UNSIGNED,
    post_id     BIGINT UNSIGNED,
    account_id  BIGINT UNSIGNED,
    reason      ENUM('disinformation', 'bot', 'coordinated', 'impersonation', 'hate', 'other') NOT NULL,
    comment     TEXT,
    status      ENUM('open', 'confirmed', 'rejected') NOT NULL DEFAULT 'open',
    reviewed_by BIGINT UNSIGNED,
    reviewed_at DATETIME(3),
    created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_flags_post (post_id),
    INDEX idx_flags_account (account_id),
    INDEX idx_flags_status (status),
    CONSTRAINT fk_flags_user FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE SET NULL,
    CONSTRAINT fk_flags_post FOREIGN KEY (post_id) REFERENCES posts (post_id) ON DELETE CASCADE,
    CONSTRAINT fk_flags_account FOREIGN KEY (account_id) REFERENCES accounts (account_id) ON DELETE CASCADE,
    CONSTRAINT fk_flags_reviewer FOREIGN KEY (reviewed_by) REFERENCES users (user_id) ON DELETE SET NULL,
    CONSTRAINT chk_flags_one_target CHECK ((post_id IS NULL) <> (account_id IS NULL))
);

CREATE TABLE IF NOT EXISTS scrape_jobs (
    job_id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    request_id        BIGINT UNSIGNED,               -- set when triggered by a dashboard request
    platform_id       SMALLINT UNSIGNED NOT NULL,
    job_type          ENUM('post', 'account', 'account_history', 'search', 'url') NOT NULL,
    target            VARCHAR(2048) NOT NULL,         -- URL, handle or search query
    account_id        BIGINT UNSIGNED,
    scraper           VARCHAR(64) NOT NULL,           -- 'oxylabs', 'telegram_api', ...
    status            ENUM('queued', 'running', 'done', 'failed') NOT NULL DEFAULT 'queued',
    items_found       INT UNSIGNED,
    error             TEXT,
    created_at        DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    started_at        DATETIME(3),
    finished_at       DATETIME(3),
    INDEX idx_scrape_jobs_status (status, created_at),
    INDEX idx_scrape_jobs_account (account_id),
    CONSTRAINT fk_scrape_jobs_request FOREIGN KEY (request_id) REFERENCES analysis_requests (request_id) ON DELETE SET NULL,
    CONSTRAINT fk_scrape_jobs_platform FOREIGN KEY (platform_id) REFERENCES platforms (platform_id),
    CONSTRAINT fk_scrape_jobs_account FOREIGN KEY (account_id) REFERENCES accounts (account_id) ON DELETE SET NULL
);

-- -----------------------------------------------------------------------------
-- Seed data (minimal)
-- -----------------------------------------------------------------------------

INSERT IGNORE INTO platforms (platform_id, code, name, base_url) VALUES
    (1, 'web',       'Website / news site', NULL),
    (2, 'telegram',  'Telegram',  'https://t.me'),
    (3, 'x',         'X (Twitter)', 'https://x.com'),
    (4, 'facebook',  'Facebook',  'https://www.facebook.com'),
    (5, 'instagram', 'Instagram', 'https://www.instagram.com'),
    (6, 'tiktok',    'TikTok',    'https://www.tiktok.com'),
    (7, 'youtube',   'YouTube',   'https://www.youtube.com'),
    (8, 'vk',        'VKontakte', 'https://vk.com');

-- EmbeddingGemma expects task prompts; stored posts and search queries use
-- different ones. Every vector in Qdrant must be made with document_prompt.
INSERT IGNORE INTO embedding_models (embedding_model_id, code, dimensions, document_prompt, query_prompt, is_active) VALUES
    (1, 'embeddinggemma-300m', 768, 'title: {title} | text: {text}', 'task: search result | query: {text}', TRUE);

INSERT IGNORE INTO schema_migrations (version) VALUES ('0001_initial');
