-- What a finalist's references said, recorded by a coordinator: up to four per nomination.
CREATE TABLE reference_notes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    finalist_category_id UUID NOT NULL REFERENCES finalist_categories (id),
    position SMALLINT NOT NULL CHECK (position BETWEEN 1 AND 4),
    referee_name TEXT NOT NULL CHECK (length(btrim(referee_name)) > 0),
    notes TEXT NOT NULL CHECK (length(btrim(notes)) > 0),
    updated_by UUID REFERENCES users (id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (finalist_category_id, position)
);
