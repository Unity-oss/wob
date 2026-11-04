-- Back to a single 1-5 scale. Ratings already made on 0-3 rubrics stay in place, so the
-- restored checks are NOT VALID: they apply to new rows only.
DROP TRIGGER ranking_entries_scale ON ranking_entries;
DROP TRIGGER calibration_ratings_scale ON calibration_ratings;
DROP TRIGGER criterion_assessments_scale ON criterion_assessments;
DROP FUNCTION check_ranking_scale();
DROP FUNCTION check_calibration_scale();
DROP FUNCTION check_assessment_scale();
DROP FUNCTION scale_bounds(UUID);

ALTER TABLE ranking_entries
    DROP CONSTRAINT ranking_entries_score_check,
    ADD CONSTRAINT ranking_entries_score_check CHECK (score BETWEEN 1 AND 5) NOT VALID;
ALTER TABLE calibration_ratings
    DROP CONSTRAINT calibration_ratings_rating_check,
    ADD CONSTRAINT calibration_ratings_rating_check CHECK (rating BETWEEN 1 AND 5) NOT VALID;
ALTER TABLE criterion_assessments
    DROP CONSTRAINT criterion_assessments_reviewer_rating_check,
    DROP CONSTRAINT criterion_assessments_rating_check,
    ADD CONSTRAINT criterion_assessments_rating_check CHECK (rating BETWEEN 1 AND 5) NOT VALID,
    ADD CONSTRAINT criterion_assessments_reviewer_rating_check
        CHECK (reviewer_rating BETWEEN 1 AND 5) NOT VALID;
ALTER TABLE rubric_versions DROP COLUMN scale;
