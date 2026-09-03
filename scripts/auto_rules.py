"""
Automated Rules Engine — Runs every 2 hours after sync.

Evaluates all active ads against kill/graduate rules and queues
recommendations in the Supabase ads_action_queue for human review.

Rules:
  KILL:  Spent >= 2x target CPL with 0 conversions → pause ad
  KILL:  CPL > 3x target for 3+ days → pause ad
  KILL:  Frequency > 4.0 (creative fatigue) → pause ad
  GRAD:  CPL <= target for 7+ days with 5+ conversions → notify for graduation

Recommendations are never executable until a human explicitly approves them.
Notifications are sent to Telegram.

Usage:
    python -m scripts.auto_rules
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import requests
from dotenv import load_dotenv
from config.audit import audit_event

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_ENV_PATH)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# --- Config ---
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
META_AD_ACCOUNT_ID = os.getenv("META_AD_ACCOUNT_ID", "")
GRAPH_BASE = f"https://graph.facebook.com/v21.0"

SUPABASE_URL = os.getenv("SUPABASE_URL_BACKOFFICE", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY_BACKOFFICE", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Thresholds (from AD_SYSTEM.md)
TARGET_CPL = 5.0  # €5 per webinar registration
MIN_SPEND_TO_JUDGE = TARGET_CPL * 2  # €10 — need 2x CPL spend before killing
CPL_KILL_MULTIPLIER = 3.0  # Kill if CPL > 3x target (€15)
FREQUENCY_KILL = 4.0  # Creative fatigue threshold
MIN_CONVERSIONS_FOR_WINNER = 5  # Need 5+ leads to declare winner
MIN_DAYS_FOR_GRADUATION = 7  # 7 days of data minimum


# --- Helpers ---

def _sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        return r.json().get("ok", False)
    except Exception:
        logger.exception("Telegram send failed")
        return False


def queue_action(action_type: str, entity_id: str, entity_name: str,
                 reason: str, params: Dict | None = None) -> bool:
    """Store a non-executable recommendation for human review."""
    merged_params = params or {}
    merged_params["reason"] = reason
    row = {
        "action_type": action_type,
        "entity_id": entity_id,
        "entity_name": f"[auto] {entity_name}",
        "params": json.dumps(merged_params),
        "status": "recommended",
        "requested_by": "rules_engine",
    }
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/ads_action_queue",
            headers={**_sb_headers(), "Prefer": "resolution=merge-duplicates"},
            json=[row],
            timeout=10,
        )
        if r.status_code in (200, 201, 204):
            audit_event("recommendation_created", action_type=action_type,
                        entity_id=entity_id, entity_name=entity_name, reason=reason,
                        metrics=merged_params)
            logger.info("Recommendation created: %s → %s (%s)", action_type, entity_name, reason)
            return True
        else:
            logger.error("Failed to queue action: %d %s", r.status_code, r.text[:200])
            return False
    except Exception:
        logger.exception("Failed to queue action")
        return False


def get_ad_performance() -> List[Dict[str, Any]]:
    """Pull performance data for all active ads from Meta API (last 7 days)."""
    # Get all active ads
    r = requests.get(
        f"{GRAPH_BASE}/{META_AD_ACCOUNT_ID}/ads",
        params={
            "fields": "id,name,effective_status,adset_id,campaign_id,created_time",
            "effective_status": '["ACTIVE"]',
            "limit": 100,
            "access_token": META_ACCESS_TOKEN,
        },
        timeout=15,
    )
    ads = r.json().get("data", [])

    results = []
    for ad in ads:
        ad_id = ad["id"]
        ad_name = ad.get("name", "?")
        created = ad.get("created_time", "")

        # Get 7-day insights
        r = requests.get(
            f"{GRAPH_BASE}/{ad_id}/insights",
            params={
                "fields": "spend,impressions,clicks,actions,cost_per_action_type,reach,frequency",
                "date_preset": "last_7d",
                "access_token": META_ACCESS_TOKEN,
            },
            timeout=15,
        )
        rows = r.json().get("data", [])

        spend = 0.0
        clicks = 0
        impressions = 0
        leads = 0
        cpl = 0.0
        frequency = 0.0
        link_clicks = 0

        if rows:
            row = rows[0]
            spend = float(row.get("spend", 0))
            clicks = int(row.get("clicks", 0))
            impressions = int(row.get("impressions", 0))
            frequency = float(row.get("frequency", 0))

            for action in row.get("actions", []):
                if action.get("action_type") in (
                    "lead", "offsite_conversion.fb_pixel_lead", "complete_registration"
                ):
                    leads += int(action.get("value", 0))
                if action.get("action_type") == "link_click":
                    link_clicks = int(action.get("value", 0))

            for cost in row.get("cost_per_action_type", []):
                if cost.get("action_type") in (
                    "lead", "offsite_conversion.fb_pixel_lead", "complete_registration"
                ):
                    cpl = float(cost.get("value", 0))

        # Calculate days running
        days_running = 0
        if created:
            try:
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00").replace("+0200", "+02:00").replace("+0100", "+01:00"))
                days_running = (datetime.now(tz=timezone.utc) - created_dt).days
            except (ValueError, TypeError):
                days_running = 0

        results.append({
            "ad_id": ad_id,
            "ad_name": ad_name,
            "adset_id": ad.get("adset_id", ""),
            "campaign_id": ad.get("campaign_id", ""),
            "spend": spend,
            "impressions": impressions,
            "clicks": clicks,
            "link_clicks": link_clicks,
            "leads": leads,
            "cpl": cpl,
            "frequency": frequency,
            "days_running": days_running,
            "ctr": (clicks / impressions * 100) if impressions > 0 else 0,
        })

    return results


def check_already_queued(entity_id: str) -> bool:
    """Check for an existing unprocessed recommendation or approval."""
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/ads_action_queue",
            headers=_sb_headers(),
            params={
                "entity_id": f"eq.{entity_id}",
                "status": "in.(recommended,approved,processing)",
            },
            timeout=10,
        )
        if r.status_code == 200:
            return len(r.json()) > 0
    except Exception:
        pass
    return False


# --- Rules ---

def evaluate_rules(ads: List[Dict[str, Any]]) -> Dict[str, List[Dict]]:
    """Evaluate kill and graduation rules against all active ads."""
    kills = []
    graduates = []
    alerts = []

    for ad in ads:
        ad_id = ad["ad_id"]
        ad_name = ad["ad_name"]
        spend = ad["spend"]
        leads = ad["leads"]
        cpl = ad["cpl"]
        frequency = ad["frequency"]
        days_running = ad["days_running"]

        # Skip if already queued
        if check_already_queued(ad_id):
            continue

        # === KILL RULES ===

        # Rule 1: Spent >= 2x target CPL with 0 conversions
        if spend >= MIN_SPEND_TO_JUDGE and leads == 0:
            kills.append({
                "ad_id": ad_id,
                "ad_name": ad_name,
                "rule": "zero_conversions",
                "reason": f"Spent €{spend:.2f} with 0 leads (threshold: €{MIN_SPEND_TO_JUDGE:.0f})",
                "spend": spend,
            })
            continue

        # Rule 2: CPL > 3x target for ads with some conversions
        if leads > 0 and cpl > TARGET_CPL * CPL_KILL_MULTIPLIER and spend >= MIN_SPEND_TO_JUDGE:
            kills.append({
                "ad_id": ad_id,
                "ad_name": ad_name,
                "rule": "cpl_too_high",
                "reason": f"CPL €{cpl:.2f} exceeds 3x target (€{TARGET_CPL * CPL_KILL_MULTIPLIER:.0f})",
                "spend": spend,
                "cpl": cpl,
            })
            continue

        # Rule 3: Creative fatigue (frequency > 4.0)
        if frequency > FREQUENCY_KILL and days_running >= 5:
            kills.append({
                "ad_id": ad_id,
                "ad_name": ad_name,
                "rule": "creative_fatigue",
                "reason": f"Frequency {frequency:.1f} (threshold: {FREQUENCY_KILL})",
                "frequency": frequency,
            })
            continue

        # === GRADUATION RULES ===

        # Rule: CPL at or below target, 5+ conversions, 7+ days of data
        if (leads >= MIN_CONVERSIONS_FOR_WINNER
                and cpl <= TARGET_CPL
                and days_running >= MIN_DAYS_FOR_GRADUATION):
            graduates.append({
                "ad_id": ad_id,
                "ad_name": ad_name,
                "rule": "winner_detected",
                "reason": f"CPL €{cpl:.2f} ≤ target €{TARGET_CPL:.0f}, {leads} leads, {days_running}d running",
                "cpl": cpl,
                "leads": leads,
                "spend": spend,
            })

        # === ALERT RULES ===

        # Alert: High spend approaching kill threshold but not there yet
        if spend >= MIN_SPEND_TO_JUDGE * 0.7 and leads == 0:
            alerts.append({
                "ad_id": ad_id,
                "ad_name": ad_name,
                "rule": "approaching_kill",
                "reason": f"€{spend:.2f} spent with 0 leads — approaching kill threshold (€{MIN_SPEND_TO_JUDGE:.0f})",
            })

    return {"kills": kills, "graduates": graduates, "alerts": alerts}


def execute_kills(kills: List[Dict]) -> int:
    """Create approval-required pause recommendations for kill decisions."""
    queued = 0
    for kill in kills:
        success = queue_action(
            action_type="pause_ad",
            entity_id=kill["ad_id"],
            entity_name=kill["ad_name"],
            reason=f"[auto_rules] {kill['rule']}: {kill['reason']}",
        )
        if success:
            queued += 1
    return queued


def generate_winner_variations(winner: Dict):
    """Queue creative generation for 3 variations of a winning ad.

    Uses the winning ad's name to infer the angle, then generates
    variations using different hooks from the same angle family.
    """
    ad_name = winner["ad_name"]
    ad_id = winner["ad_id"]
    cpl = winner.get("cpl", 0)
    leads = winner.get("leads", 0)

    # Map ad names to variation prompts (based on AD_SYSTEM.md angles)
    variation_sets = {
        "3-Path": [
            "3 revenue paths most AI builders miss. Which one fits you?",
            "SaaS, RaaS, or Services? Pick your AI revenue path.",
            "The system behind €200K in AI revenue. 3 paths explained.",
        ],
        "90%": [
            "9 out of 10 AI builders quit before making €1. Here's why.",
            "The #1 mistake AI builders make (and the fix).",
            "Why most AI products fail — and 3 that print money.",
        ],
        "Testimonial": [
            "No coding experience. Built a full SaaS product. Real story.",
            "Agency restructured their entire workflow after this. Real quote.",
            "From years of research to a shipped product. Real builder.",
        ],
        "Stop": [
            "Tutorials won't pay your bills. This system will.",
            "You don't need another course. You need a system.",
            "Building is easy. Selling is where the money is.",
        ],
        "Live Demo": [
            "15 minutes. One AI product. Built live. No code.",
            "Watch an AI product go from zero to deployed in 15 min.",
            "Can you build a sellable AI product in 15 minutes? I'll prove it.",
        ],
        "Math": [
            "3 clients × €500/mo. That's €18K/year from one weekend project.",
            "What if your side project paid your rent? The math works.",
            "One AI product. 3 clients. €1,500/month recurring. Here's how.",
        ],
    }

    # Find matching variation set
    matched_hooks = None
    for key, hooks in variation_sets.items():
        if key.lower() in ad_name.lower():
            matched_hooks = hooks
            break

    if not matched_hooks:
        # Default: use general high-performing hooks
        matched_hooks = [
            "The system that turned AI skills into €200K+ revenue.",
            "From builder to business owner in 30 days. Free workshop.",
            "I'll build a real AI product live. Then show you how to sell it.",
        ]

    # Queue creative generation for each variation
    for i, hook in enumerate(matched_hooks):
        queue_action(
            action_type="generate_creative",
            entity_id=ad_id,
            entity_name=f"Variation {i+1} of winner: {ad_name}",
            reason=f"Winner variation — original CPL €{cpl:.2f} with {leads} leads",
            params={
                "prompt": hook,
                "source_ad_id": ad_id,
                "source_ad_name": ad_name,
                "variation_number": i + 1,
            },
        )

    logger.info("Queued 3 creative variations for winner: %s", ad_name)


def notify_results(kills: List[Dict], graduates: List[Dict], alerts: List[Dict]):
    """Send a Telegram notification summarizing rule outcomes."""
    if not kills and not graduates and not alerts:
        logger.info("No rules triggered — all clear")
        return

    lines = ["🤖 *Auto Rules Engine*\n"]

    if kills:
        lines.append(f"*🔴 {len(kills)} ad pause recommendation(s):*")
        for k in kills:
            lines.append(f"  • _{k['ad_name']}_")
            lines.append(f"    {k['reason']}")
        lines.append("")

    if graduates:
        lines.append(f"*🏆 {len(graduates)} winner(s) detected:*")
        for g in graduates:
            lines.append(f"  • _{g['ad_name']}_")
            lines.append(f"    {g['reason']}")
        lines.append("  → Consider moving to scale campaign")
        lines.append("")

    if alerts:
        lines.append(f"*⚠️ {len(alerts)} alert(s):*")
        for a in alerts:
            lines.append(f"  • _{a['ad_name']}_: {a['reason']}")

    send_telegram("\n".join(lines))


def check_budget_allocation():
    """Check if budget allocation has drifted >10% from 70/20/10 target.

    Compares actual spend distribution across scale/iterate/test campaigns
    against the Hormozi target and queues budget adjustments if needed.
    """
    # Load campaign IDs
    import json as _json
    data_dir = Path(__file__).resolve().parent.parent / "data"
    campaign_ids = _json.loads((data_dir / "campaign_ids.json").read_text())

    target_split = {"scale": 0.70, "iterate": 0.20, "test": 0.10}

    # Get spend per campaign (last 7 days)
    total_spend = 0.0
    campaign_spend = {}

    for ctype, cid in campaign_ids.items():
        if ctype == "retarget":
            continue
        r = requests.get(
            f"{GRAPH_BASE}/{cid}/insights",
            params={
                "fields": "spend",
                "date_preset": "last_7d",
                "access_token": META_ACCESS_TOKEN,
            },
            timeout=15,
        )
        rows = r.json().get("data", [])
        spend = float(rows[0].get("spend", 0)) if rows else 0.0
        campaign_spend[ctype] = spend
        total_spend += spend

    if total_spend < 10:
        logger.info("Budget check: total spend €%.2f too low to rebalance", total_spend)
        return

    # Check drift
    drifts = {}
    for ctype, target_pct in target_split.items():
        actual_pct = campaign_spend.get(ctype, 0) / total_spend if total_spend > 0 else 0
        drift = abs(actual_pct - target_pct)
        drifts[ctype] = {"actual": actual_pct, "target": target_pct, "drift": drift}

    max_drift = max(d["drift"] for d in drifts.values())

    if max_drift > 0.10:
        lines = ["📊 *Budget Drift Alert*\n"]
        for ctype, d in drifts.items():
            emoji = "⚠️" if d["drift"] > 0.10 else "✅"
            lines.append(
                f"  {emoji} {ctype}: {d['actual']*100:.0f}% actual vs {d['target']*100:.0f}% target"
            )
        lines.append(f"\n_Max drift: {max_drift*100:.0f}% — consider rebalancing_")
        send_telegram("\n".join(lines))
        logger.info("Budget drift detected: max %.0f%%", max_drift * 100)
    else:
        logger.info("Budget allocation OK (max drift: %.0f%%)", max_drift * 100)


def budget_guardian():
    """Budget Guardian — runs every hour to enforce hard spending limits.

    Reads limits from knowledge/budget-rules.md.
    It only creates approval-required recommendations; it never pauses ads.
    """
    # Hard caps (from budget-rules.md)
    MAX_DAILY_TOTAL = 120.00      # €120/day across all ad sets
    MAX_SINGLE_ADSET = 60.00      # €60/day per ad set
    EMERGENCY_PAUSE_AT = 150.00   # Pause everything if daily spend exceeds this

    logger.info("Budget Guardian: checking limits...")

    # Get today's total spend
    r = requests.get(
        f"{GRAPH_BASE}/{META_AD_ACCOUNT_ID}/insights",
        params={"fields": "spend", "date_preset": "today", "access_token": META_ACCESS_TOKEN},
        timeout=15,
    )
    rows = r.json().get("data", [])
    today_spend = float(rows[0].get("spend", 0)) if rows else 0

    # EMERGENCY: recommend pausing active ad sets; never mutate Meta here.
    if today_spend > EMERGENCY_PAUSE_AT:
        logger.error("EMERGENCY: today's spend €%.2f exceeds €%.0f — recommending pause", today_spend, EMERGENCY_PAUSE_AT)
        send_telegram(
            f"🚨 *EMERGENCY — Budget Guardian*\n\n"
            f"Today's spend: €{today_spend:.2f} (limit: €{EMERGENCY_PAUSE_AT:.0f})\n\n"
            f"*Human approval required before any pause.*\n"
            f"Review budgets and recommendations in the dashboard."
        )

        # Create one recommendation for each active ad set
        r = requests.get(
            f"{GRAPH_BASE}/{META_AD_ACCOUNT_ID}/adsets",
            params={
                "fields": "id,name",
                "effective_status": '["ACTIVE"]',
                "limit": 10,
                "access_token": META_ACCESS_TOKEN,
            },
            timeout=15,
        )
        for adset in r.json().get("data", []):
            queue_action(
                "pause_adset", adset["id"], adset.get("name", adset["id"]),
                f"Emergency spend recommendation: today €{today_spend:.2f} > €{EMERGENCY_PAUSE_AT:.2f}",
                {"today_spend": today_spend, "threshold": EMERGENCY_PAUSE_AT},
            )
        return

    # Check each ad set's budget against the cap
    r = requests.get(
        f"{GRAPH_BASE}/{META_AD_ACCOUNT_ID}/adsets",
        params={
            "fields": "id,name,daily_budget,effective_status",
            "effective_status": '["ACTIVE"]',
            "limit": 10,
            "access_token": META_ACCESS_TOKEN,
        },
        timeout=15,
    )
    adsets = r.json().get("data", [])

    total_budget = 0
    issues = []

    for adset in adsets:
        budget_eur = int(adset.get("daily_budget", 0)) / 100
        total_budget += budget_eur

        if budget_eur > MAX_SINGLE_ADSET:
            issues.append(f"⚠️ {adset['name']}: €{budget_eur:.0f}/day (max: €{MAX_SINGLE_ADSET:.0f})")
            logger.warning("  Ad set '%s' budget €%.0f exceeds max €%.0f",
                          adset["name"], budget_eur, MAX_SINGLE_ADSET)

    if total_budget > MAX_DAILY_TOTAL:
        issues.append(f"⚠️ Total budget: €{total_budget:.0f}/day (max: €{MAX_DAILY_TOTAL:.0f})")

    if issues:
        send_telegram(
            f"⚠️ *Budget Guardian Alert*\n\n"
            + "\n".join(issues) +
            f"\n\nToday's spend so far: €{today_spend:.2f}\n"
            f"Fix in Ads Manager: ads.manager.facebook.com"
        )
        logger.warning("Budget issues detected: %d", len(issues))
    else:
        logger.info("  All budgets within limits (total: €%.0f/day, spent today: €%.2f)", total_budget, today_spend)


def main():
    logger.info("Running auto rules engine...")

    # 1. Pull current performance
    ads = get_ad_performance()
    logger.info("Fetched performance for %d active ads", len(ads))

    if not ads:
        logger.info("No active ads found — nothing to evaluate")
        return

    # 2. Evaluate rules
    results = evaluate_rules(ads)
    kills = results["kills"]
    graduates = results["graduates"]
    alerts = results["alerts"]

    logger.info(
        "Rules result: %d kills, %d graduates, %d alerts",
        len(kills), len(graduates), len(alerts),
    )

    # 3. Create recommendations (never execute here)
    if kills:
        queued = execute_kills(kills)
        logger.info("Created %d pause recommendations", queued)

    # 4. Handle graduates — notify + queue creative variations
    for grad in graduates:
        # Queue 3 creative variations based on the winning ad's angle
        generate_winner_variations(grad)

    # 5. Notify
    notify_results(kills, graduates, alerts)

    # 6. Check budget allocation drift
    try:
        check_budget_allocation()
    except Exception:
        logger.exception("Budget check failed (non-critical)")

    # 7. Budget Guardian — detect limit breaches and recommend actions
    try:
        budget_guardian()
    except Exception:
        logger.exception("Budget guardian failed (non-critical)")

    # 8. Summary
    print(f"\n{'='*50}")
    print(f"Auto Rules Summary:")
    print(f"  Ads evaluated: {len(ads)}")
    print(f"  Pause recommendations: {len(kills)}")
    print(f"  Winners found: {len(graduates)}")
    print(f"  Alerts:        {len(alerts)}")
    print(f"{'='*50}")


if __name__ == "__main__":
    if not META_ACCESS_TOKEN or not SUPABASE_URL:
        logger.error("Missing META_ACCESS_TOKEN or SUPABASE_URL_BACKOFFICE")
        exit(1)
    main()
