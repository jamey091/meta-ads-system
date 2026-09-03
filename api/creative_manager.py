"""
Manage ad creatives: upload images/videos, create AdCreatives, list & inspect.

All uploads and creative creation are logged for audit trail.
"""

import logging
import os
import time
from typing import Any

from facebook_business.adobjects.adaccount import AdAccount
from facebook_business.adobjects.adcreative import AdCreative
from facebook_business.adobjects.adimage import AdImage
from facebook_business.adobjects.advideo import AdVideo
from facebook_business.exceptions import FacebookRequestError

from api.meta_client import MetaClient
from config.settings import META_AD_ACCOUNT_ID
from config.safety import block_direct_write

logger = logging.getLogger(__name__)

# Video processing poll settings
_VIDEO_POLL_INTERVAL = 5  # seconds
_VIDEO_POLL_MAX_WAIT = 300  # seconds (5 min)


# ==================================================================
# Image upload
# ==================================================================


def upload_image(image_path: str) -> dict[str, Any]:
    """Upload an image and return its hash.

    Parameters
    ----------
    image_path : str
        Absolute or relative path to the image file (JPG/PNG).

    Returns
    -------
    dict
        ``{"image_hash": "...", "url": "...", "filename": "..."}``

    Raises
    ------
    FileNotFoundError
        If the image file does not exist.
    FacebookRequestError
        On API failure.
    """
    if not os.path.isfile(image_path):
        raise FileNotFoundError(f"Image file not found: {image_path}")

    block_direct_write("upload_image")
    client = MetaClient()
    image = AdImage(parent_id=META_AD_ACCOUNT_ID)
    image[AdImage.Field.filename] = image_path

    try:
        image.remote_create()

        image_hash = image.get(AdImage.Field.hash, "")
        image_url = image.get(AdImage.Field.url, "")

        logger.info(
            "AUDIT | UPLOADED image '%s' -> hash=%s",
            os.path.basename(image_path), image_hash,
        )
        return {
            "image_hash": image_hash,
            "url": image_url,
            "filename": os.path.basename(image_path),
        }
    except FacebookRequestError as exc:
        logger.error(
            "Failed to upload image '%s': [%s] %s",
            image_path, exc.api_error_code(), exc.api_error_message(),
        )
        raise


# ==================================================================
# Video upload
# ==================================================================


def upload_video(video_path: str, wait_for_encoding: bool = True) -> dict[str, Any]:
    """Upload a video and return its ID.

    Parameters
    ----------
    video_path : str
        Path to the video file (MP4 recommended).
    wait_for_encoding : bool
        If ``True`` (default), poll until Meta finishes encoding the
        video.  This is necessary before the video can be used in a
        creative.

    Returns
    -------
    dict
        ``{"video_id": "...", "status": "...", "filename": "..."}``

    Raises
    ------
    FileNotFoundError
        If the video file does not exist.
    TimeoutError
        If encoding does not finish within the poll window.
    """
    if not os.path.isfile(video_path):
        raise FileNotFoundError(f"Video file not found: {video_path}")

    block_direct_write("upload_video")
    client = MetaClient()
    account = client.get_account()

    try:
        video = AdVideo(parent_id=META_AD_ACCOUNT_ID)
        video[AdVideo.Field.filepath] = video_path
        video.remote_create()

        video_id = video.get("id", "")
        logger.info(
            "AUDIT | UPLOADED video '%s' -> id=%s (encoding started)",
            os.path.basename(video_path), video_id,
        )

        if wait_for_encoding and video_id:
            status = _wait_for_video_encoding(video_id)
        else:
            status = "processing"

        return {
            "video_id": video_id,
            "status": status,
            "filename": os.path.basename(video_path),
        }
    except FacebookRequestError as exc:
        logger.error(
            "Failed to upload video '%s': [%s] %s",
            video_path, exc.api_error_code(), exc.api_error_message(),
        )
        raise


def _wait_for_video_encoding(video_id: str) -> str:
    """Poll Meta until a video is ready (or timeout).

    Returns the final encoding status string.
    """
    client = MetaClient()
    video = AdVideo(video_id)
    elapsed = 0

    while elapsed < _VIDEO_POLL_MAX_WAIT:
        try:
            info = client.rate_limited_request(
                video.api_get, fields=["status"]
            )
            video_status = info.get("status", {})
            encoding_status = (
                video_status.get("video_status", "processing")
                if isinstance(video_status, dict)
                else str(video_status)
            )

            if encoding_status == "ready":
                logger.info("Video %s encoding complete.", video_id)
                return "ready"

            if encoding_status == "error":
                logger.error("Video %s encoding failed.", video_id)
                return "error"

        except FacebookRequestError:
            logger.warning("Poll for video %s status failed, retrying...", video_id)

        time.sleep(_VIDEO_POLL_INTERVAL)
        elapsed += _VIDEO_POLL_INTERVAL

    logger.warning(
        "Video %s still encoding after %ds — returning as 'processing'.",
        video_id, _VIDEO_POLL_MAX_WAIT,
    )
    raise TimeoutError(
        f"Video {video_id} did not finish encoding within {_VIDEO_POLL_MAX_WAIT}s"
    )


# ==================================================================
# Creative creation
# ==================================================================


def create_creative(
    name: str,
    body: str,
    title: str,
    link_url: str,
    image_hash: str | None = None,
    video_id: str | None = None,
    call_to_action: str | None = None,
    page_id: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Create an AdCreative with the given assets and copy.

    Supply either ``image_hash`` (for image ads) or ``video_id``
    (for video ads).  If neither is provided, a link-share ad with
    no media is created (unusual but valid for some placements).

    Parameters
    ----------
    name : str
        Creative display name (internal label).
    body : str
        Primary text (the main ad copy above the media).
    title : str
        Headline shown below the media.
    link_url : str
        Destination URL when users click the ad.
    image_hash : str, optional
        Hash returned by :func:`upload_image`.
    video_id : str, optional
        ID returned by :func:`upload_video`.
    call_to_action : str, optional
        CTA button type.  Examples: ``LEARN_MORE``, ``SHOP_NOW``,
        ``SIGN_UP``, ``DOWNLOAD``, ``GET_OFFER``, ``BOOK_TRAVEL``.
        Defaults to ``LEARN_MORE`` if not specified.
    page_id : str, optional
        Facebook Page ID to publish from.  Required by Meta for most
        ad formats.  If not provided, the code expects it in the
        ``META_PAGE_ID`` env var.
    description : str, optional
        Link description (smaller text below the headline).

    Returns
    -------
    dict
        Created creative metadata including ``id``.
    """
    block_direct_write("create_creative")
    client = MetaClient()
    account = client.get_account()

    if page_id is None:
        page_id = os.environ.get("META_PAGE_ID", "")
        if not page_id:
            logger.warning(
                "No page_id provided and META_PAGE_ID env var is empty. "
                "Creative creation may fail."
            )

    cta_type = call_to_action or "LEARN_MORE"

    # Build the object_story_spec (the actual ad content structure)
    link_data: dict[str, Any] = {
        "message": body,
        "name": title,
        "link": link_url,
        "call_to_action": {
            "type": cta_type,
            "value": {"link": link_url},
        },
    }

    if description:
        link_data["description"] = description

    if image_hash:
        link_data["image_hash"] = image_hash
    elif video_id:
        # For video ads, use video_data instead of link_data
        video_data: dict[str, Any] = {
            "video_id": video_id,
            "message": body,
            "title": title,
            "link_description": description or "",
            "call_to_action": {
                "type": cta_type,
                "value": {"link": link_url},
            },
        }

        object_story_spec: dict[str, Any] = {
            "page_id": page_id,
            "video_data": video_data,
        }

        params: dict[str, Any] = {
            "name": name,
            "object_story_spec": object_story_spec,
        }

        try:
            result = client.rate_limited_request(
                account.create_ad_creative, params=params
            )
            creative_data = dict(result)
            creative_id = creative_data.get("id", "")
            logger.info(
                "AUDIT | CREATED video creative '%s' (id=%s, video=%s, cta=%s)",
                name, creative_id, video_id, cta_type,
            )
            return creative_data
        except FacebookRequestError as exc:
            logger.error(
                "Failed to create video creative '%s': [%s] %s",
                name, exc.api_error_code(), exc.api_error_message(),
            )
            raise

    # Image or no-media path
    object_story_spec = {
        "page_id": page_id,
        "link_data": link_data,
    }

    params = {
        "name": name,
        "object_story_spec": object_story_spec,
    }

    try:
        result = client.rate_limited_request(
            account.create_ad_creative, params=params
        )
        creative_data = dict(result)
        creative_id = creative_data.get("id", "")

        media_type = "image" if image_hash else "link"
        logger.info(
            "AUDIT | CREATED %s creative '%s' (id=%s, cta=%s)",
            media_type, name, creative_id, cta_type,
        )
        return creative_data
    except FacebookRequestError as exc:
        logger.error(
            "Failed to create creative '%s': [%s] %s",
            name, exc.api_error_code(), exc.api_error_message(),
        )
        raise


# ==================================================================
# List & inspect creatives
# ==================================================================


def list_creatives(
    campaign_id: str | None = None,
) -> list[dict[str, Any]]:
    """List creatives with their metadata.

    Parameters
    ----------
    campaign_id : str, optional
        If provided, only creatives used by ads in this campaign.
        Otherwise all creatives in the account.

    Returns
    -------
    list[dict]
    """
    # Delegate to the insights_fetcher implementation which already
    # handles the campaign-scoped vs account-scoped logic.
    from api.insights_fetcher import fetch_ad_creatives

    return fetch_ad_creatives(campaign_id=campaign_id)


def get_creative_details(creative_id: str) -> dict[str, Any]:
    """Get full creative details including preview URL.

    Parameters
    ----------
    creative_id : str
        The AdCreative ID.

    Returns
    -------
    dict
        Full creative metadata.  Includes ``effective_object_story_id``,
        ``object_story_spec``, ``thumbnail_url``, and ``preview_url``
        (the ad preview shareable link, if available).
    """
    client = MetaClient()
    creative = AdCreative(creative_id)

    fields = [
        "id",
        "name",
        "body",
        "title",
        "image_url",
        "image_hash",
        "video_id",
        "thumbnail_url",
        "effective_object_story_id",
        "object_story_spec",
        "call_to_action_type",
        "status",
        "url_tags",
        "link_url",
    ]

    try:
        info = client.rate_limited_request(creative.api_get, fields=fields)
        data = dict(info)

        # Try to get ad preview URL
        try:
            previews = client.rate_limited_request(
                creative.get_previews,
                params={"ad_format": "DESKTOP_FEED_STANDARD"},
            )
            preview_list = client.exhaust_pagination(previews)
            if preview_list:
                data["preview_html"] = preview_list[0].get("body", "")
        except FacebookRequestError:
            logger.debug(
                "Could not fetch preview for creative %s (non-critical)",
                creative_id,
            )
            data["preview_html"] = ""

        logger.info("Fetched details for creative %s", creative_id)
        return data

    except FacebookRequestError as exc:
        logger.error(
            "Failed to get creative details for %s: [%s] %s",
            creative_id, exc.api_error_code(), exc.api_error_message(),
        )
        raise
