import sys
import types
import unittest
from unittest.mock import Mock, patch

if "requests" not in sys.modules:
    sys.modules["requests"] = Mock()
if "dotenv" not in sys.modules:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv

from scripts import process_actions


class ProcessActionsTests(unittest.TestCase):
    def action(self):
        return {
            "id": "action-1", "action_type": "pause_ad", "entity_id": "123",
            "entity_name": "Test ad", "status": "approved",
            "approved_by": "operator@example.test", "approved_at": "2020-01-01T00:00:00Z",
            "approval_id": "approval-1",
        }

    @patch.object(process_actions, "audit_event")
    @patch.object(process_actions, "_update_action")
    @patch.object(process_actions, "_get_approved_actions")
    def test_default_dry_run_never_calls_handler(self, get_actions, update, audit):
        handler = Mock()
        get_actions.return_value = [self.action()]
        with patch.dict(process_actions.HANDLERS, {"pause_ad": handler}, clear=True), \
             patch.dict("os.environ", {}, clear=True):
            result = process_actions.process_all()
        handler.assert_not_called()
        update.assert_not_called()
        audit.assert_called_once()
        self.assertEqual(result["skipped"], 1)

    @patch.object(process_actions, "audit_event")
    @patch.object(process_actions, "_update_action")
    @patch.object(process_actions, "_get_approved_actions")
    def test_live_flags_still_reject_expired_approval(self, get_actions, update, audit):
        handler = Mock()
        get_actions.return_value = [self.action()]
        env = {"DRY_RUN": "false", "META_WRITE_ENABLED": "true",
               "META_ALLOWED_ENTITY_IDS": "123"}
        with patch.dict(process_actions.HANDLERS, {"pause_ad": handler}, clear=True), \
             patch.dict("os.environ", env, clear=True):
            result = process_actions.process_all()
        handler.assert_not_called()
        update.assert_called_once()
        self.assertEqual(update.call_args.args[1], "blocked")
        self.assertEqual(result["skipped"], 1)


if __name__ == "__main__":
    unittest.main()
