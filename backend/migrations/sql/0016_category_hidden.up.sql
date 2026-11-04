-- A hidden category (e.g. a smoke test) drops out of lists, the dashboard and the judges' pack.
-- Nothing in it is changed or deleted, and an administrator can show it again.
ALTER TABLE award_categories ADD COLUMN hidden_at TIMESTAMPTZ;
