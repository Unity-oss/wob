-- When a criterion source was last matched against the candidate's claims, so earlier research
-- is matched once rather than on every run.
ALTER TABLE external_sources ADD COLUMN claims_matched_at TIMESTAMPTZ;
