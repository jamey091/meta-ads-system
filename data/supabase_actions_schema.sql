-- ============================================================
-- Ads Action Queue — Execute actions from the dashboard
-- Run this in your backoffice Supabase SQL Editor
-- ============================================================

CREATE TABLE IF NOT EXISTS ads_action_queue (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    -- What to do
    action_type text NOT NULL,  -- pause_ad / activate_ad / pause_adset / activate_adset / pause_campaign / activate_campaign / update_budget / refresh_sync / generate_creative

    -- What to do it to
    entity_id text,             -- Meta ad/adset/campaign ID
    entity_name text,           -- Human-readable name (for display)
    entity_type text,           -- ad / adset / campaign

    -- Parameters (for budget changes, creative generation, etc.)
    params jsonb DEFAULT '{}',  -- e.g. {"new_budget": 2000} or {"prompt": "..."}

    -- Status tracking
    status text NOT NULL DEFAULT 'recommended'
        CHECK (status IN ('recommended', 'approved', 'processing', 'completed', 'blocked', 'failed', 'rejected')),
    result text,                    -- Success message or error details

    -- Who/when
    requested_by text DEFAULT 'dashboard',
    requested_at timestamptz DEFAULT now(),
    approved_by text,
    approved_at timestamptz,
    approval_id uuid,
    processed_at timestamptz,

    -- For display in the dashboard
    display_message text,          -- "Recommend pausing ad '90% Never Make Money'..."

    CONSTRAINT approved_action_has_metadata CHECK (
        status <> 'approved' OR
        (approved_by IS NOT NULL AND approved_at IS NOT NULL AND approval_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_action_queue_status ON ads_action_queue(status, requested_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_action_queue_approval_id
    ON ads_action_queue(approval_id) WHERE approval_id IS NOT NULL;

-- RLS
ALTER TABLE ads_action_queue ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow authenticated read" ON ads_action_queue FOR SELECT TO authenticated USING (true);
CREATE POLICY "Allow authenticated insert" ON ads_action_queue FOR INSERT TO authenticated WITH CHECK (true);
CREATE POLICY "Service role full access" ON ads_action_queue FOR ALL TO service_role USING (true);
