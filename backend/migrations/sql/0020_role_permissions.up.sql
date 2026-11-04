-- Rights an administrator grants each user group (app/security/permissions.py). Administrators
-- hold every right, so they have no rows. Seeded with the rights each group had until now.
CREATE TABLE role_permissions (
    role user_role NOT NULL CHECK (role <> 'admin'),
    permission TEXT NOT NULL CHECK (
        permission IN (
            'manage_evidence',
            'review_claims',
            'approve_rubrics',
            'generate_reports',
            'manage_calibration',
            'rate_calibration',
            'export_personal_data'
        )
    ),
    PRIMARY KEY (role, permission)
);

INSERT INTO role_permissions (role, permission) VALUES
    ('secretariat', 'manage_evidence'),
    ('secretariat', 'review_claims'),
    ('secretariat', 'generate_reports'),
    ('secretariat', 'manage_calibration'),
    ('secretariat', 'export_personal_data'),
    ('reviewer', 'review_claims'),
    ('reviewer', 'rate_calibration'),
    ('judge', 'rate_calibration'),
    ('committee', 'approve_rubrics'),
    ('committee', 'generate_reports'),
    ('committee', 'manage_calibration'),
    ('committee', 'rate_calibration');
