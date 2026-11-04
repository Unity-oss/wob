-- A finding that supports its claim or criterion, comes from an official source or the
-- organisation's own website, and is certainly about this nominee is accepted without a reviewer.
-- A reviewer can still reject it with notes; auto_accepted stays true as the record of how it
-- was first accepted.
ALTER TABLE external_sources
    ADD COLUMN auto_accepted BOOLEAN NOT NULL DEFAULT false,
    DROP CONSTRAINT external_sources_review_complete,
    ADD CONSTRAINT external_sources_review_complete CHECK (
        CASE reviewer_outcome
            WHEN 'pending' THEN reviewed_by IS NULL AND reviewed_at IS NULL
            WHEN 'accepted' THEN reviewed_at IS NOT NULL AND (reviewed_by IS NOT NULL OR auto_accepted)
            ELSE reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL
        END
    ),
    ADD CONSTRAINT external_sources_auto_accept_rules CHECK (
        NOT auto_accepted
        OR (reviewer_outcome <> 'pending'
            AND identity_confidence = 'high'
            AND relation IS DISTINCT FROM 'contradicts'
            AND source_type IN (
                'official_registry', 'regulator', 'organisation_website', 'annual_report',
                'professional_body'
            ))
    );
