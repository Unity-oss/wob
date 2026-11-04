-- Findings still standing on automatic acceptance go back to awaiting review.
ALTER TABLE external_sources
    DROP CONSTRAINT external_sources_auto_accept_rules,
    DROP CONSTRAINT external_sources_review_complete;

DELETE FROM claim_evidence
WHERE external_source_id IN (
    SELECT id FROM external_sources
    WHERE auto_accepted AND reviewer_outcome = 'accepted' AND reviewed_by IS NULL
);
UPDATE external_sources
SET reviewer_outcome = 'pending', reviewed_at = NULL, credibility = NULL
WHERE auto_accepted AND reviewer_outcome = 'accepted' AND reviewed_by IS NULL;

ALTER TABLE external_sources
    DROP COLUMN auto_accepted,
    ADD CONSTRAINT external_sources_review_complete CHECK (
        (reviewer_outcome = 'pending') = (reviewed_by IS NULL AND reviewed_at IS NULL)
    );
