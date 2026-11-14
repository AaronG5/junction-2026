-- =============================================================================
-- 0003: keep the values added in 0002 sensible.
-- One change per statement, so a half-applied run can safely be repeated.
-- =============================================================================

-- Map positions are 0-100 (NULL = not placed yet; CHECK lets NULL through).
ALTER TABLE post_clusters ADD CONSTRAINT chk_post_clusters_map_x CHECK (map_x BETWEEN 0 AND 100);
ALTER TABLE post_clusters ADD CONSTRAINT chk_post_clusters_map_y CHECK (map_y BETWEEN 0 AND 100);

-- A narrative can't be last seen before it was first seen.
ALTER TABLE clusters ADD CONSTRAINT chk_clusters_seen_order CHECK (first_seen <= last_seen);

-- Week-on-week change can be huge for a narrative that was tiny last week
-- (DECIMAL(7,2) stops at 99999.99 %). Repeating this is harmless.
ALTER TABLE clusters MODIFY COLUMN change_pct DECIMAL(12, 2) NULL;
