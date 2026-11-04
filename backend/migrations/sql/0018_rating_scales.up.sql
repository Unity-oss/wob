-- The board moved to a 0-3 scale (0 None, 1 Poor, 2 Good, 3 Excellent). Each rubric version
-- records its scale: existing versions keep 1-5, so their ratings, rankings and reports stay
-- valid, and new versions default to 0-3.
ALTER TABLE rubric_versions
    ADD COLUMN scale TEXT NOT NULL DEFAULT 'one_to_five'
        CHECK (scale IN ('one_to_five', 'zero_to_three'));
ALTER TABLE rubric_versions ALTER COLUMN scale SET DEFAULT 'zero_to_three';

CREATE FUNCTION scale_bounds(rubric UUID, OUT low SMALLINT, OUT high SMALLINT)
LANGUAGE sql STABLE AS $$
    SELECT CASE scale WHEN 'zero_to_three' THEN 0 ELSE 1 END::SMALLINT,
           CASE scale WHEN 'zero_to_three' THEN 3 ELSE 5 END::SMALLINT
    FROM rubric_versions WHERE id = rubric
$$;

-- The column checks become the outer bounds of every scale; triggers hold each value to its
-- own rubric version's scale.
ALTER TABLE criterion_assessments
    DROP CONSTRAINT criterion_assessments_rating_check,
    DROP CONSTRAINT criterion_assessments_reviewer_rating_check,
    ADD CONSTRAINT criterion_assessments_rating_check CHECK (rating BETWEEN 0 AND 5),
    ADD CONSTRAINT criterion_assessments_reviewer_rating_check
        CHECK (reviewer_rating BETWEEN 0 AND 5);
ALTER TABLE calibration_ratings
    DROP CONSTRAINT calibration_ratings_rating_check,
    ADD CONSTRAINT calibration_ratings_rating_check CHECK (rating BETWEEN 0 AND 5);
ALTER TABLE ranking_entries
    DROP CONSTRAINT ranking_entries_score_check,
    ADD CONSTRAINT ranking_entries_score_check CHECK (score BETWEEN 0 AND 5);

CREATE FUNCTION check_assessment_scale() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    b RECORD;
BEGIN
    SELECT s.* INTO b FROM assessment_runs r, scale_bounds(r.rubric_version_id) s
    WHERE r.id = NEW.run_id;
    IF NEW.rating NOT BETWEEN b.low AND b.high
       OR NEW.reviewer_rating NOT BETWEEN b.low AND b.high THEN
        RAISE EXCEPTION 'rating outside the rubric''s %-% scale', b.low, b.high
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER criterion_assessments_scale
    BEFORE INSERT OR UPDATE OF rating, reviewer_rating ON criterion_assessments
    FOR EACH ROW EXECUTE FUNCTION check_assessment_scale();

CREATE FUNCTION check_calibration_scale() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    b RECORD;
BEGIN
    SELECT s.* INTO b
    FROM calibration_items i
    JOIN calibration_studies st ON st.id = i.study_id, scale_bounds(st.rubric_version_id) s
    WHERE i.id = NEW.item_id;
    IF NEW.rating NOT BETWEEN b.low AND b.high THEN
        RAISE EXCEPTION 'rating outside the rubric''s %-% scale', b.low, b.high
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER calibration_ratings_scale
    BEFORE INSERT OR UPDATE OF rating ON calibration_ratings
    FOR EACH ROW EXECUTE FUNCTION check_calibration_scale();

CREATE FUNCTION check_ranking_scale() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    b RECORD;
BEGIN
    SELECT s.* INTO b FROM ranking_snapshots r, scale_bounds(r.rubric_version_id) s
    WHERE r.id = NEW.snapshot_id;
    IF NEW.score NOT BETWEEN b.low AND b.high THEN
        RAISE EXCEPTION 'score outside the rubric''s %-% scale', b.low, b.high
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER ranking_entries_scale
    BEFORE INSERT ON ranking_entries
    FOR EACH ROW EXECUTE FUNCTION check_ranking_scale();
