-- Undo automatic verifications: claims whose current status came from one go back to unverified,
-- and the automatic decisions are removed (append-only rows, so via the purge path).
SET LOCAL wob.purge = 'on';
UPDATE claims SET status = 'unverified'
WHERE id IN (SELECT claim_id FROM verification_reviews WHERE basis IS NOT NULL)
  AND NOT EXISTS (
    SELECT 1 FROM verification_reviews v
    WHERE v.claim_id = claims.id AND v.basis IS NULL
      AND v.created_at > (SELECT max(created_at) FROM verification_reviews a
                          WHERE a.claim_id = claims.id AND a.basis IS NOT NULL)
);
DELETE FROM verification_reviews WHERE basis IS NOT NULL;
ALTER TABLE verification_reviews
    DROP CONSTRAINT verification_reviews_basis_verifies,
    DROP CONSTRAINT verification_reviews_reviewer_or_basis,
    ALTER COLUMN reviewer_id SET NOT NULL,
    DROP COLUMN basis;
