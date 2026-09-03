import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from config.safety import SafetyError, SafetyPolicy, authorize_action, block_direct_write


def approved(action_type="pause_ad", **overrides):
    row = {
        "action_type": action_type,
        "entity_id": "123",
        "status": "approved",
        "approved_by": "operator@example.test",
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "approval_id": "00000000-0000-0000-0000-000000000001",
        "params": {},
    }
    row.update(overrides)
    return row


class SafetyPolicyTests(unittest.TestCase):
    def policy(self, **overrides):
        values = dict(
            dry_run=False, meta_write_enabled=True,
            budget_writes_enabled=False, activation_writes_enabled=False,
            allowed_entity_ids=frozenset({"123"}), approval_ttl_minutes=60,
            max_budget_change_pct=20, max_daily_budget_cents=10000,
        )
        values.update(overrides)
        return SafetyPolicy(**values)

    def test_defaults_fail_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            policy = SafetyPolicy.from_env()
        self.assertTrue(policy.dry_run)
        self.assertFalse(policy.meta_write_enabled)
        self.assertFalse(policy.budget_writes_enabled)

    def test_valid_pause_requires_all_approval_fields(self):
        authorize_action(approved(), self.policy())
        for field in ("approved_by", "approved_at", "approval_id"):
            row = approved()
            row[field] = ""
            with self.subTest(field=field), self.assertRaises(SafetyError):
                authorize_action(row, self.policy())

    def test_expired_approval_is_blocked(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        with self.assertRaisesRegex(SafetyError, "expired"):
            authorize_action(approved(approved_at=old), self.policy())

    def test_activation_has_separate_switch(self):
        with self.assertRaisesRegex(SafetyError, "Activation"):
            authorize_action(approved("activate_ad"), self.policy())

    def test_budget_requires_caps_and_old_value(self):
        policy = self.policy(budget_writes_enabled=True)
        with self.assertRaises(SafetyError):
            authorize_action(approved("update_budget", params={"new_budget": 1200}), policy)
        with self.assertRaisesRegex(SafetyError, "MAX_BUDGET_CHANGE_PCT"):
            authorize_action(approved("update_budget", params={"old_budget": 1000, "new_budget": 1500}), policy)
        authorize_action(approved("update_budget", params={"old_budget": 1000, "new_budget": 1200}), policy)

    def test_direct_writes_are_always_blocked(self):
        with self.assertRaisesRegex(SafetyError, "approve an action-queue"):
            block_direct_write("pause_ad")


if __name__ == "__main__":
    unittest.main()
