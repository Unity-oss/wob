-- The jury decided (2026-10-23) that supporting documents are a credible source of truth, so a
-- claim one supports is verified without a reviewer. Such a decision records its basis instead
-- of a reviewer; it can only verify, and a reviewer can still change it later.
ALTER TABLE verification_reviews
    ADD COLUMN basis TEXT CHECK (basis IN ('jury_supporting_documents')),
    ALTER COLUMN reviewer_id DROP NOT NULL,
    ADD CONSTRAINT verification_reviews_reviewer_or_basis CHECK (
        reviewer_id IS NOT NULL OR basis IS NOT NULL
    ),
    ADD CONSTRAINT verification_reviews_basis_verifies CHECK (
        basis IS NULL OR decision = 'verified'
    );
