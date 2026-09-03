"""
Create, update, and manage Meta ad campaigns, ad sets, and ads.

All mutating operations are logged for audit trail.  Campaigns default
to PAUSED for safety — nothing goes live without explicit activation.

Uses the Advantage+ (unified) campaign structure where applicable
(the standard for 2026 Meta API).
"""

import logging
from typing import Any

from facebook_business.adobjects.adaccount import AdAccount
from facebook_business.adobjects.campaign import Campaign
from facebook_business.adobjects.adset import AdSet
from facebook_business.adobjects.ad import Ad
from facebook_business.exceptions import FacebookRequestError

from api.meta_client import MetaClient
from config.settings import META_AD_ACCOUNT_ID
from config.safety import block_direct_write

logger = logging.getLogger(__name__)


# ==================================================================
# Campaign operations
# ==================================================================


def create_campaign(
    name: str,
    objective: str,
    budget: float,
    status: str = "PAUSED",
    special_ad_categories: list[str] | None = None,
    buying_type: str = "AUCTION",
) -> dict[str, Any]:
    """Create a new campaign (defaults to PAUSED for safety).

    Parameters
    ----------
    name : str
        Campaign display name.
    objective : str
        Campaign objective.  Common 2026 values:
        ``OUTCOME_SALES``, ``OUTCOME_LEADS``, ``OUTCOME_ENGAGEMENT``,
        ``OUTCOME_AWARENESS``, ``OUTCOME_TRAFFIC``, ``OUTCOME_APP_PROMOTION``.
    budget : float
        Daily budget in the account currency (in cents for Meta — we
        convert to micro-units internally so callers pass normal amounts).
    status : str
        ``PAUSED`` (default) or ``ACTIVE``.
    special_ad_categories : list[str], optional
        E.g. ``["EMPLOYMENT"]``, ``["HOUSING"]``, ``["CREDIT"]``.
        Empty list by default (required field in 2026 API).
    buying_type : str
        ``AUCTION`` (default) or ``RESERVED``.

    Returns
    -------
    dict
        Created campaign metadata including ``id``.
    """
    block_direct_write("create_campaign")
    client = MetaClient()
    account = client.get_account()

    if special_ad_categories is None:
        special_ad_categories = []

    # Meta expects budget in cents (integer)
    daily_budget_cents = str(int(round(budget * 100)))

    params: dict[str, Any] = {
        "name": name,
        "objective": objective,
        "status": status,
        "special_ad_categories": special_ad_categories,
        "buying_type": buying_type,
        "daily_budget": daily_budget_cents,
        # Advantage+ campaign budget (formerly CBO).  Setting this at
        # campaign level lets Meta optimise across ad sets.
        "smart_promotion_type": "GUIDED_CREATION",
    }

    try:
        result = client.rate_limited_request(account.create_campaign, params=params)
        campaign_data = dict(result)
        campaign_id = campaign_data.get("id", "")

        logger.info(
            "AUDIT | CREATED campaign '%s' (id=%s, objective=%s, "
            "daily_budget=%s, status=%s)",
            name, campaign_id, objective, budget, status,
        )
        return campaign_data
    except FacebookRequestError as exc:
        logger.error(
            "Failed to create campaign '%s': [%s] %s",
            name, exc.api_error_code(), exc.api_error_message(),
        )
        raise


def list_campaigns(
    status_filter: list[str] | None = None,
) -> list[dict[str, Any]]:
    """List all campaigns in the account.

    Parameters
    ----------
    status_filter : list[str], optional
        Only return campaigns with these statuses, e.g.
        ``["ACTIVE", "PAUSED"]``.  ``None`` returns all.

    Returns
    -------
    list[dict]
    """
    client = MetaClient()
    account = client.get_account()

    fields = [
        "id",
        "name",
        "objective",
        "status",
        "effective_status",
        "daily_budget",
        "lifetime_budget",
        "buying_type",
        "created_time",
        "updated_time",
        "start_time",
        "stop_time",
        "special_ad_categories",
        "smart_promotion_type",
    ]

    params: dict[str, Any] = {}
    if status_filter:
        params["effective_status"] = status_filter

    try:
        cursor = client.rate_limited_request(
            account.get_campaigns, fields=fields, params=params
        )
        results = client.exhaust_pagination(cursor)
        logger.info("Listed %d campaigns", len(results))
        return results
    except FacebookRequestError as exc:
        logger.error(
            "Failed to list campaigns: [%s] %s",
            exc.api_error_code(), exc.api_error_message(),
        )
        raise


def get_campaign_status(campaign_id: str) -> dict[str, Any]:
    """Check a campaign's delivery status, learning-phase info, etc.

    Parameters
    ----------
    campaign_id : str
        The campaign ID.

    Returns
    -------
    dict
        Includes ``effective_status``, ``issues_info``, ``configured_status``,
        and ``is_learning_phase`` flag derived from issues info.
    """
    client = MetaClient()
    campaign = Campaign(campaign_id)

    fields = [
        "id",
        "name",
        "status",
        "effective_status",
        "configured_status",
        "issues_info",
        "daily_budget",
        "lifetime_budget",
        "budget_remaining",
    ]

    try:
        info = client.rate_limited_request(campaign.api_get, fields=fields)
        data = dict(info)

        # Determine if any ad sets are in learning phase
        adsets_cursor = client.rate_limited_request(
            campaign.get_ad_sets,
            fields=["id", "name", "effective_status", "learning_phase_info"],
        )
        adsets = client.exhaust_pagination(adsets_cursor)

        learning_adsets = []
        for adset in adsets:
            lp_info = adset.get("learning_phase_info", {})
            if isinstance(lp_info, dict) and lp_info.get("status") == "LEARNING":
                learning_adsets.append({
                    "adset_id": adset.get("id"),
                    "adset_name": adset.get("name"),
                })

        data["is_learning_phase"] = len(learning_adsets) > 0
        data["learning_phase_adsets"] = learning_adsets
        data["total_adsets"] = len(adsets)

        return data
    except FacebookRequestError as exc:
        logger.error(
            "Failed to get campaign status for %s: [%s] %s",
            campaign_id, exc.api_error_code(), exc.api_error_message(),
        )
        raise


# ==================================================================
# Ad Set operations
# ==================================================================


def create_adset(
    campaign_id: str,
    name: str,
    targeting: dict[str, Any],
    budget: float,
    optimization_goal: str,
    billing_event: str = "IMPRESSIONS",
    status: str = "PAUSED",
    start_time: str | None = None,
    end_time: str | None = None,
    bid_strategy: str = "LOWEST_COST_WITHOUT_CAP",
) -> dict[str, Any]:
    """Create an ad set within a campaign.

    Parameters
    ----------
    campaign_id : str
        Parent campaign ID.
    name : str
        Ad set display name.
    targeting : dict
        Meta targeting spec.  Example::

            {
                "geo_locations": {"countries": ["NL"]},
                "age_min": 25,
                "age_max": 55,
                "targeting_automation": {
                    "advantage_audience": 1  # Advantage+ audience
                },
            }
    budget : float
        Daily budget in account currency.
    optimization_goal : str
        E.g. ``OFFSITE_CONVERSIONS``, ``LEAD_GENERATION``, ``LINK_CLICKS``,
        ``REACH``, ``IMPRESSIONS``, ``VALUE``.
    billing_event : str
        ``IMPRESSIONS`` (default), ``LINK_CLICKS``, etc.
    status : str
        ``PAUSED`` (default) or ``ACTIVE``.
    start_time : str, optional
        ISO 8601 datetime.  If omitted the ad set starts immediately
        when activated.
    end_time : str, optional
        ISO 8601 datetime.
    bid_strategy : str
        ``LOWEST_COST_WITHOUT_CAP`` (default), ``COST_CAP``,
        ``LOWEST_COST_WITH_BID_CAP``.

    Returns
    -------
    dict
        Created ad set metadata including ``id``.
    """
    block_direct_write("create_adset")
    client = MetaClient()
    account = client.get_account()

    daily_budget_cents = str(int(round(budget * 100)))

    params: dict[str, Any] = {
        "campaign_id": campaign_id,
        "name": name,
        "targeting": targeting,
        "daily_budget": daily_budget_cents,
        "optimization_goal": optimization_goal,
        "billing_event": billing_event,
        "bid_strategy": bid_strategy,
        "status": status,
    }

    if start_time:
        params["start_time"] = start_time
    if end_time:
        params["end_time"] = end_time

    try:
        result = client.rate_limited_request(account.create_ad_set, params=params)
        adset_data = dict(result)
        adset_id = adset_data.get("id", "")

        logger.info(
            "AUDIT | CREATED ad set '%s' (id=%s, campaign=%s, "
            "daily_budget=%s, optimization=%s, status=%s)",
            name, adset_id, campaign_id, budget, optimization_goal, status,
        )
        return adset_data
    except FacebookRequestError as exc:
        logger.error(
            "Failed to create ad set '%s': [%s] %s",
            name, exc.api_error_code(), exc.api_error_message(),
        )
        raise


def list_adsets(
    campaign_id: str | None = None,
    status_filter: list[str] | None = None,
) -> list[dict[str, Any]]:
    """List ad sets, optionally filtered by campaign and/or status.

    Parameters
    ----------
    campaign_id : str, optional
        If provided, only ad sets in this campaign.
    status_filter : list[str], optional
        E.g. ``["ACTIVE", "PAUSED"]``.

    Returns
    -------
    list[dict]
    """
    client = MetaClient()

    fields = [
        "id",
        "name",
        "campaign_id",
        "status",
        "effective_status",
        "daily_budget",
        "lifetime_budget",
        "optimization_goal",
        "billing_event",
        "bid_strategy",
        "targeting",
        "learning_phase_info",
        "created_time",
        "updated_time",
        "start_time",
        "end_time",
    ]

    params: dict[str, Any] = {}
    if status_filter:
        params["effective_status"] = status_filter

    try:
        if campaign_id:
            campaign = Campaign(campaign_id)
            cursor = client.rate_limited_request(
                campaign.get_ad_sets, fields=fields, params=params
            )
        else:
            account = client.get_account()
            cursor = client.rate_limited_request(
                account.get_ad_sets, fields=fields, params=params
            )
        results = client.exhaust_pagination(cursor)
        logger.info("Listed %d ad sets", len(results))
        return results
    except FacebookRequestError as exc:
        logger.error(
            "Failed to list ad sets: [%s] %s",
            exc.api_error_code(), exc.api_error_message(),
        )
        raise


# ==================================================================
# Ad operations
# ==================================================================


def create_ad(
    adset_id: str,
    name: str,
    creative_id: str,
    status: str = "PAUSED",
) -> dict[str, Any]:
    """Create an ad within an ad set.

    Parameters
    ----------
    adset_id : str
        Parent ad set ID.
    name : str
        Ad display name.
    creative_id : str
        ID of the AdCreative to use.
    status : str
        ``PAUSED`` (default) or ``ACTIVE``.

    Returns
    -------
    dict
        Created ad metadata including ``id``.
    """
    block_direct_write("create_ad")
    client = MetaClient()
    account = client.get_account()

    params: dict[str, Any] = {
        "adset_id": adset_id,
        "name": name,
        "creative": {"creative_id": creative_id},
        "status": status,
    }

    try:
        result = client.rate_limited_request(account.create_ad, params=params)
        ad_data = dict(result)
        ad_id = ad_data.get("id", "")

        logger.info(
            "AUDIT | CREATED ad '%s' (id=%s, adset=%s, creative=%s, status=%s)",
            name, ad_id, adset_id, creative_id, status,
        )
        return ad_data
    except FacebookRequestError as exc:
        logger.error(
            "Failed to create ad '%s': [%s] %s",
            name, exc.api_error_code(), exc.api_error_message(),
        )
        raise


def list_ads(
    adset_id: str | None = None,
    status_filter: list[str] | None = None,
) -> list[dict[str, Any]]:
    """List ads, optionally filtered by ad set and/or status.

    Parameters
    ----------
    adset_id : str, optional
        If provided, only ads in this ad set.
    status_filter : list[str], optional
        E.g. ``["ACTIVE", "PAUSED"]``.

    Returns
    -------
    list[dict]
    """
    client = MetaClient()

    fields = [
        "id",
        "name",
        "adset_id",
        "campaign_id",
        "status",
        "effective_status",
        "creative",
        "created_time",
        "updated_time",
    ]

    params: dict[str, Any] = {}
    if status_filter:
        params["effective_status"] = status_filter

    try:
        if adset_id:
            adset = AdSet(adset_id)
            cursor = client.rate_limited_request(
                adset.get_ads, fields=fields, params=params
            )
        else:
            account = client.get_account()
            cursor = client.rate_limited_request(
                account.get_ads, fields=fields, params=params
            )
        results = client.exhaust_pagination(cursor)
        logger.info("Listed %d ads", len(results))
        return results
    except FacebookRequestError as exc:
        logger.error(
            "Failed to list ads: [%s] %s",
            exc.api_error_code(), exc.api_error_message(),
        )
        raise


# ==================================================================
# Status toggles
# ==================================================================


def pause_ad(ad_id: str) -> dict[str, Any]:
    """Pause an ad.

    Parameters
    ----------
    ad_id : str
        The ad ID to pause.

    Returns
    -------
    dict
        Updated ad data.
    """
    block_direct_write("pause_ad")
    client = MetaClient()
    ad = Ad(ad_id)

    try:
        result = client.rate_limited_request(
            ad.api_update, params={"status": "PAUSED"}
        )
        logger.info("AUDIT | PAUSED ad id=%s", ad_id)
        return {"id": ad_id, "status": "PAUSED", "success": True}
    except FacebookRequestError as exc:
        logger.error(
            "Failed to pause ad %s: [%s] %s",
            ad_id, exc.api_error_code(), exc.api_error_message(),
        )
        raise


def activate_ad(ad_id: str) -> dict[str, Any]:
    """Activate (unpause) an ad.

    Parameters
    ----------
    ad_id : str
        The ad ID to activate.

    Returns
    -------
    dict
        Updated ad data.
    """
    block_direct_write("activate_ad")
    client = MetaClient()
    ad = Ad(ad_id)

    try:
        result = client.rate_limited_request(
            ad.api_update, params={"status": "ACTIVE"}
        )
        logger.info("AUDIT | ACTIVATED ad id=%s", ad_id)
        return {"id": ad_id, "status": "ACTIVE", "success": True}
    except FacebookRequestError as exc:
        logger.error(
            "Failed to activate ad %s: [%s] %s",
            ad_id, exc.api_error_code(), exc.api_error_message(),
        )
        raise


def pause_adset(adset_id: str) -> dict[str, Any]:
    """Pause an ad set.

    Parameters
    ----------
    adset_id : str
        The ad set ID to pause.

    Returns
    -------
    dict
        Updated ad set data.
    """
    block_direct_write("pause_adset")
    client = MetaClient()
    adset = AdSet(adset_id)

    try:
        result = client.rate_limited_request(
            adset.api_update, params={"status": "PAUSED"}
        )
        logger.info("AUDIT | PAUSED ad set id=%s", adset_id)
        return {"id": adset_id, "status": "PAUSED", "success": True}
    except FacebookRequestError as exc:
        logger.error(
            "Failed to pause ad set %s: [%s] %s",
            adset_id, exc.api_error_code(), exc.api_error_message(),
        )
        raise


def activate_adset(adset_id: str) -> dict[str, Any]:
    """Activate (unpause) an ad set.

    Parameters
    ----------
    adset_id : str
        The ad set ID to activate.

    Returns
    -------
    dict
        Updated ad set data.
    """
    block_direct_write("activate_adset")
    client = MetaClient()
    adset = AdSet(adset_id)

    try:
        result = client.rate_limited_request(
            adset.api_update, params={"status": "ACTIVE"}
        )
        logger.info("AUDIT | ACTIVATED ad set id=%s", adset_id)
        return {"id": adset_id, "status": "ACTIVE", "success": True}
    except FacebookRequestError as exc:
        logger.error(
            "Failed to activate ad set %s: [%s] %s",
            adset_id, exc.api_error_code(), exc.api_error_message(),
        )
        raise


# ==================================================================
# Budget management
# ==================================================================


def update_budget(
    entity_id: str,
    entity_type: str,
    new_budget: float,
) -> dict[str, Any]:
    """Change the daily budget for a campaign or ad set.

    Parameters
    ----------
    entity_id : str
        Campaign or ad set ID.
    entity_type : str
        ``"campaign"`` or ``"adset"``.
    new_budget : float
        New daily budget in account currency.

    Returns
    -------
    dict
        Confirmation with old and new budget information.
    """
    block_direct_write("update_budget")
    client = MetaClient()
    budget_cents = str(int(round(new_budget * 100)))

    entity_type_lower = entity_type.lower()

    try:
        if entity_type_lower == "campaign":
            obj = Campaign(entity_id)
            # Read current budget first for audit
            current = client.rate_limited_request(
                obj.api_get, fields=["daily_budget", "name"]
            )
            current_data = dict(current)
            old_budget = current_data.get("daily_budget", "unknown")

            client.rate_limited_request(
                obj.api_update, params={"daily_budget": budget_cents}
            )

            logger.info(
                "AUDIT | BUDGET CHANGE campaign '%s' (id=%s): %s -> %s",
                current_data.get("name", ""), entity_id, old_budget, budget_cents,
            )

        elif entity_type_lower == "adset":
            obj = AdSet(entity_id)
            current = client.rate_limited_request(
                obj.api_get, fields=["daily_budget", "name"]
            )
            current_data = dict(current)
            old_budget = current_data.get("daily_budget", "unknown")

            client.rate_limited_request(
                obj.api_update, params={"daily_budget": budget_cents}
            )

            logger.info(
                "AUDIT | BUDGET CHANGE ad set '%s' (id=%s): %s -> %s",
                current_data.get("name", ""), entity_id, old_budget, budget_cents,
            )
        else:
            raise ValueError(
                f"entity_type must be 'campaign' or 'adset', got '{entity_type}'"
            )

        return {
            "id": entity_id,
            "entity_type": entity_type_lower,
            "old_budget_cents": old_budget,
            "new_budget_cents": budget_cents,
            "new_budget": new_budget,
            "success": True,
        }

    except FacebookRequestError as exc:
        logger.error(
            "Failed to update budget for %s %s: [%s] %s",
            entity_type, entity_id, exc.api_error_code(), exc.api_error_message(),
        )
        raise
