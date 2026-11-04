-- Each judge's own score (0-3) and comment for a finalist, scored independently.
CREATE TABLE judge_scores (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    finalist_category_id UUID NOT NULL REFERENCES finalist_categories (id),
    judge_id UUID NOT NULL REFERENCES users (id),
    score SMALLINT NOT NULL CHECK (score BETWEEN 0 AND 3),
    comment TEXT NOT NULL CHECK (length(btrim(comment)) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (finalist_category_id, judge_id)
);

-- Closing scoring stops judges changing their scores once the winner is chosen.
ALTER TABLE award_categories
    ADD COLUMN judging_closed_at TIMESTAMPTZ,
    ADD COLUMN judging_closed_by UUID REFERENCES users (id);

ALTER TABLE role_permissions DROP CONSTRAINT role_permissions_permission_check;
ALTER TABLE role_permissions ADD CONSTRAINT role_permissions_permission_check CHECK (
    permission IN (
        'manage_evidence',
        'review_claims',
        'approve_rubrics',
        'generate_reports',
        'manage_calibration',
        'rate_calibration',
        'export_personal_data',
        'score_candidates',
        'view_judge_scores'
    )
);
-- Judges score independently, so they never see the tally.
ALTER TABLE role_permissions ADD CONSTRAINT role_permissions_judges_blind CHECK (
    NOT (role = 'judge' AND permission = 'view_judge_scores')
);

INSERT INTO role_permissions (role, permission) VALUES
    ('judge', 'score_candidates'),
    ('secretariat', 'view_judge_scores'),
    ('committee', 'view_judge_scores');
