# Safe Phase 1/2 operation

This branch treats Meta as read-only by default. It syncs performance data and
stores recommendations, but no rule is allowed to pause, activate, create, or
change the budget of a Meta entity.

## Safe first run

1. Copy `.env.example` to `.env` and add your credentials locally. Never commit it.
2. Keep `DRY_RUN=true`, `META_WRITE_ENABLED=false`,
   `META_BUDGET_WRITES_ENABLED=false`, and
   `META_ACTIVATION_WRITES_ENABLED=false`.
3. Run `python -m scripts.check_config`. This performs no network calls.
4. Apply `data/supabase_ads_schema.sql`, `data/supabase_actions_schema.sql`, and
   the other schemas needed by your reporting features. For an existing action
   table, apply `data/supabase_safety_migration.sql`; it converts old pending
   actions into non-executable recommendations.
5. Run `python -m scripts.sync_to_supabase` to fetch Meta insights and write
   reporting rows to Supabase. Meta calls in this path are GET-only.
6. Run `python -m scripts.auto_rules`. It stores recommendations with status
   `recommended`; it never changes Meta state.

The included `.github/workflows/read-only-sync.yml` automates only steps 5 and
6. It deliberately never invokes the action processor. Keep its GitHub
environment restricted to a read-only Meta token.

## Approval workflow (Phase 2)

Review the recommendation and its metrics. To approve one operation, set its
status to `approved` and provide `approved_by`, a timezone-aware `approved_at`,
and a unique `approval_id`. Approvals expire after `APPROVAL_TTL_MINUTES`.

The processor selects only `approved` rows. With the safe defaults it records a
local `execution_simulated` audit event and leaves the row untouched. A real
write additionally requires `DRY_RUN=false`, `META_WRITE_ENABLED=true`, and the
entity to be in `META_ALLOWED_ENTITY_IDS`. Activation and budget writes require
their own flags. Budget approval must contain `old_budget` and `new_budget` in
cents and pass both the percentage and absolute caps.

Enabling these flags is an operator decision outside Phase 1/2. Never schedule
the action processor while testing. Prefer a manually dispatched job with a
protected environment and two-person review.

## Audit and recovery

Safety decisions are appended as JSON Lines to `data/audit.jsonl` (configurable
with `AUDIT_LOG_PATH`). Secret-like fields are redacted. Retain these logs in a
write-once store in production. If an approved action is incorrect, reject it
before execution. If a mutation has already happened, restore the previous
status or budget in Meta Ads Manager; the audit record contains the entity and
correlation identifiers needed for review.

## Required Meta permission

For Phase 1, use a token scoped to `ads_read` whenever possible. Do not grant
`ads_management` until you intentionally enter a controlled execution phase.
