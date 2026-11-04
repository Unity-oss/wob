DELETE FROM role_permissions WHERE permission IN ('score_candidates', 'view_judge_scores');
ALTER TABLE role_permissions DROP CONSTRAINT role_permissions_judges_blind;
ALTER TABLE role_permissions DROP CONSTRAINT role_permissions_permission_check;
ALTER TABLE role_permissions ADD CONSTRAINT role_permissions_permission_check CHECK (
    permission IN (
        'manage_evidence',
        'review_claims',
        'approve_rubrics',
        'generate_reports',
        'manage_calibration',
        'rate_calibration',
        'export_personal_data'
    )
);
ALTER TABLE award_categories DROP COLUMN judging_closed_by, DROP COLUMN judging_closed_at;
DROP TABLE judge_scores;
