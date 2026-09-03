"""Fail-closed runtime safety policy for Meta Ads mutations."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


TRUTHY = {"1", "true", "yes", "on"}
MUTATING_ACTIONS = {
    "pause_ad", "activate_ad", "pause_adset", "activate_adset",
    "pause_campaign", "activate_campaign", "update_budget",
}
ACTIVATION_ACTIONS = {"activate_ad", "activate_adset", "activate_campaign"}


class SafetyError(RuntimeError):
    """Raised when a requested operation fails the safety policy."""


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in TRUTHY:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise SafetyError(f"{name} must be true or false")


def _csv(name: str) -> set[str]:
    return {item.strip() for item in os.getenv(name, "").split(",") if item.strip()}


@dataclass(frozen=True)
class SafetyPolicy:
    dry_run: bool
    meta_write_enabled: bool
    budget_writes_enabled: bool
    activation_writes_enabled: bool
    allowed_entity_ids: frozenset[str]
    approval_ttl_minutes: int
    max_budget_change_pct: float
    max_daily_budget_cents: int

    @classmethod
    def from_env(cls) -> "SafetyPolicy":
        try:
            ttl = int(os.getenv("APPROVAL_TTL_MINUTES", "60"))
            max_pct = float(os.getenv("MAX_BUDGET_CHANGE_PCT", "20"))
            max_budget = int(os.getenv("MAX_DAILY_BUDGET_CENTS", "0"))
        except ValueError as exc:
            raise SafetyError(f"Invalid numeric safety setting: {exc}") from exc
        if ttl <= 0 or max_pct <= 0 or max_budget < 0:
            raise SafetyError("Safety limits must be positive (budget cap may be 0 to block all)")
        return cls(
            dry_run=env_bool("DRY_RUN", True),
            meta_write_enabled=env_bool("META_WRITE_ENABLED", False),
            budget_writes_enabled=env_bool("META_BUDGET_WRITES_ENABLED", False),
            activation_writes_enabled=env_bool("META_ACTIVATION_WRITES_ENABLED", False),
            allowed_entity_ids=frozenset(_csv("META_ALLOWED_ENTITY_IDS")),
            approval_ttl_minutes=ttl,
            max_budget_change_pct=max_pct,
            max_daily_budget_cents=max_budget,
        )


def validate_read_environment(env: Mapping[str, str] | None = None) -> list[str]:
    values = env or os.environ
    errors: list[str] = []
    token = values.get("META_ACCESS_TOKEN", "").strip()
    account = values.get("META_AD_ACCOUNT_ID", "").strip()
    if not token or token.startswith("your_"):
        errors.append("META_ACCESS_TOKEN is missing or still a placeholder")
    if not re.fullmatch(r"act_\d+", account):
        errors.append("META_AD_ACCOUNT_ID must match act_<digits>")
    return errors


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise SafetyError("approved_at is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SafetyError("approved_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise SafetyError("approved_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def authorize_action(action: Mapping[str, Any], policy: SafetyPolicy | None = None) -> None:
    """Validate the two-key runtime flags and a fresh, explicit approval record."""
    policy = policy or SafetyPolicy.from_env()
    action_type = str(action.get("action_type", ""))
    entity_id = str(action.get("entity_id", ""))
    if action_type not in MUTATING_ACTIONS:
        raise SafetyError(f"Action type is not an allowed Meta mutation: {action_type}")
    if policy.dry_run:
        raise SafetyError("DRY_RUN is enabled")
    if not policy.meta_write_enabled:
        raise SafetyError("META_WRITE_ENABLED is disabled")
    if action.get("status") != "approved":
        raise SafetyError("Action status must be approved")
    if not str(action.get("approved_by", "")).strip():
        raise SafetyError("approved_by is required")
    if not str(action.get("approval_id", "")).strip():
        raise SafetyError("approval_id is required")
    age = datetime.now(timezone.utc) - _parse_time(action.get("approved_at"))
    if age.total_seconds() < 0 or age.total_seconds() > policy.approval_ttl_minutes * 60:
        raise SafetyError("Approval is expired or dated in the future")
    if not entity_id or (policy.allowed_entity_ids and entity_id not in policy.allowed_entity_ids):
        raise SafetyError("Entity is missing or not in META_ALLOWED_ENTITY_IDS")
    if action_type in ACTIVATION_ACTIONS and not policy.activation_writes_enabled:
        raise SafetyError("Activation writes are disabled")
    if action_type == "update_budget":
        if not policy.budget_writes_enabled:
            raise SafetyError("Budget writes are disabled")
        params = action.get("params") or {}
        if isinstance(params, str):
            import json
            params = json.loads(params)
        new_budget = int(params.get("new_budget", 0))
        old_budget = int(params.get("old_budget", 0))
        if not old_budget or not new_budget:
            raise SafetyError("Budget approval requires old_budget and new_budget in cents")
        pct = abs(new_budget - old_budget) / old_budget * 100
        if pct > policy.max_budget_change_pct:
            raise SafetyError("Budget change exceeds MAX_BUDGET_CHANGE_PCT")
        if not policy.max_daily_budget_cents or new_budget > policy.max_daily_budget_cents:
            raise SafetyError("New budget exceeds MAX_DAILY_BUDGET_CENTS")


def block_direct_write(operation: str) -> None:
    """Prevent library callers from bypassing the approved action queue."""
    raise SafetyError(
        f"Direct Meta write '{operation}' is disabled in Phase 1/2; "
        "create and approve an action-queue record instead"
    )
