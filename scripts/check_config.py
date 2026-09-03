"""Validate local configuration without making network requests."""

import os
from pathlib import Path

from dotenv import load_dotenv

from config.safety import SafetyError, SafetyPolicy, validate_read_environment

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def main() -> int:
    errors = validate_read_environment()
    try:
        policy = SafetyPolicy.from_env()
    except SafetyError as exc:
        errors.append(str(exc))
        policy = None
    for name in ("SUPABASE_URL_BACKOFFICE", "SUPABASE_SERVICE_KEY_BACKOFFICE"):
        value = os.getenv(name, "").strip()
        if not value or "your_" in value or "your-" in value:
            errors.append(f"{name} is missing or still a placeholder")
    if errors:
        print("Configuration is not ready:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Configuration is valid.")
    print(f"Safety mode: dry_run={policy.dry_run}, meta_write_enabled={policy.meta_write_enabled}")
    if policy.dry_run or not policy.meta_write_enabled:
        print("Meta mutations are blocked (recommended Phase 1/2 mode).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

