-- CPU-first candidate; lifecycle cancellation and re-activation are not integrated.
CREATE TABLE IF NOT EXISTS mwrev_review (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    installation bigint NOT NULL,
    repository bigint NOT NULL,
    pr bigint NOT NULL,
    head text NOT NULL,
    base text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    dispatch_after timestamptz NOT NULL DEFAULT now(),
    dispatch_token uuid,
    dispatch_attempts integer NOT NULL DEFAULT 0,
    modal_call_id text,
    attempt_token uuid,
    expires_at timestamptz,
    result jsonb,
    UNIQUE (installation, repository, pr, head, base)
);
CREATE TABLE IF NOT EXISTS mwrev_delivery (
    installation bigint NOT NULL,
    repository bigint NOT NULL,
    delivery text NOT NULL,
    review_id bigint NOT NULL REFERENCES mwrev_review(id),
    PRIMARY KEY (installation, repository, delivery)
);
CREATE INDEX IF NOT EXISTS mwrev_pending
ON mwrev_review (dispatch_after, id)
WHERE modal_call_id IS NULL AND result IS NULL;

ALTER TABLE mwrev_review ADD COLUMN IF NOT EXISTS comment_id bigint;
ALTER TABLE mwrev_review ADD COLUMN IF NOT EXISTS publishing_after timestamptz NOT NULL DEFAULT now();
ALTER TABLE mwrev_review ADD COLUMN IF NOT EXISTS published_at timestamptz;
ALTER TABLE mwrev_review ADD COLUMN IF NOT EXISTS model_attempts integer NOT NULL DEFAULT 0;
