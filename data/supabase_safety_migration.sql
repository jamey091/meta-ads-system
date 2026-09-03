-- Apply this to an existing deployment before using the safe action processor.
ALTER TABLE ads_action_queue ALTER COLUMN status SET DEFAULT 'recommended';
ALTER TABLE ads_action_queue ADD COLUMN IF NOT EXISTS approved_by text;
ALTER TABLE ads_action_queue ADD COLUMN IF NOT EXISTS approved_at timestamptz;
ALTER TABLE ads_action_queue ADD COLUMN IF NOT EXISTS approval_id uuid;

-- Existing unprocessed actions must not become executable after migration.
UPDATE ads_action_queue SET status = 'recommended' WHERE status = 'pending';

CREATE UNIQUE INDEX IF NOT EXISTS idx_action_queue_approval_id
    ON ads_action_queue(approval_id) WHERE approval_id IS NOT NULL;

DO $$ BEGIN
    ALTER TABLE ads_action_queue ADD CONSTRAINT approved_action_has_metadata CHECK (
        status <> 'approved' OR
        (approved_by IS NOT NULL AND approved_at IS NOT NULL AND approval_id IS NOT NULL)
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
