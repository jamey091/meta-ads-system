"""Structured, append-only audit events with conservative secret redaction."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)
_SECRET_KEYS = re.compile(r"token|secret|password|authorization|apikey|api_key", re.I)


def _redact(value: Any, key: str = "") -> Any:
    if _SECRET_KEYS.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def audit_event(event: str, **details: Any) -> dict[str, Any]:
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_id": str(uuid.uuid4()),
        "event": event,
        **_redact(details),
    }
    path = Path(os.getenv("AUDIT_LOG_PATH", "data/audit.jsonl"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    logger.info("AUDIT event=%s id=%s", event, record["event_id"])
    return record

