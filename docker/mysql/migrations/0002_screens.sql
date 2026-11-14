-- =============================================================================
-- 0002: what the dashboard screens need that init.sql lacks.
-- Runs once per database (see backend/veritas/db/migrations.py).
-- One change per statement, so a half-applied run can safely be repeated.
-- =============================================================================

-- Position of each post on the cluster map: 0-100 with 2 decimals.
-- NULL until a clustering run has placed the post.
ALTER TABLE post_clusters ADD COLUMN map_x DECIMAL(5, 2) NULL;
ALTER TABLE post_clusters ADD COLUMN map_y DECIMAL(5, 2) NULL;

-- The claim that sets a narrative's status (its main claim's verdict).
ALTER TABLE clusters ADD COLUMN main_claim_id BIGINT UNSIGNED NULL;
ALTER TABLE clusters ADD CONSTRAINT fk_clusters_main_claim
    FOREIGN KEY (main_claim_id) REFERENCES claims (claim_id) ON DELETE SET NULL;

-- Summary numbers for the narratives list, written by the clustering run so
-- the list doesn't recompute them on every request.
ALTER TABLE clusters ADD COLUMN change_pct DECIMAL(7, 2) NULL;   -- NULL = new this week
ALTER TABLE clusters ADD COLUMN accounts INT UNSIGNED NOT NULL DEFAULT 0;
ALTER TABLE clusters ADD COLUMN first_seen DATE NULL;
ALTER TABLE clusters ADD COLUMN last_seen DATE NULL;

-- Key/value settings shared by everyone using this install (data source
-- choice, sharing). Values are JSON.
CREATE TABLE IF NOT EXISTS settings (
    setting_key VARCHAR(64) PRIMARY KEY,
    value       JSON NOT NULL,
    updated_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3)
);
