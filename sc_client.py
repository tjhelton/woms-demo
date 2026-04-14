"""SafetyCulture API client for Actions."""

from __future__ import annotations

import httpx
import logging
import os

def _headers() -> dict:
    token = os.getenv("SC_API_TOKEN", "")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _client() -> httpx.AsyncClient:
    base = os.getenv("SC_API_BASE", "https://api.safetyculture.io")
    return httpx.AsyncClient(
        base_url=base,
        headers=_headers(),
        timeout=30.0,
    )


async def list_actions(page_size: int = 50, page_token: str | None = None) -> dict:
    """List actions with optional pagination."""
    body: dict = {"page_size": page_size}
    if page_token:
        body["page_token"] = page_token
    async with _client() as client:
        resp = await client.post("/tasks/v1/actions/list", json=body)
        resp.raise_for_status()
        return resp.json()


async def get_action(action_id: str) -> dict:
    """Get a single action by ID."""
    async with _client() as client:
        resp = await client.get(f"/tasks/v1/actions/{action_id}")
        resp.raise_for_status()
        return resp.json()


async def action_exists(action_id: str) -> bool:
    """Check if an action still exists in SC. Returns False on 404 (deleted)."""
    async with _client() as client:
        resp = await client.get(f"/tasks/v1/actions/{action_id}")
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True


async def create_action(
    title: str,
    description: str = "",
    priority_id: str | None = None,
    status_id: str | None = None,
    due_at: str | None = None,
    site_id: str | None = None,
    collaborators: list[dict] | None = None,
) -> dict:
    """Create a new action in SafetyCulture."""
    body: dict = {"title": title}
    if description:
        body["description"] = description
    if priority_id:
        body["priority_id"] = priority_id
    if status_id:
        body["status_id"] = status_id
    if due_at:
        # SC API requires full ISO 8601 timestamp, not bare dates
        if len(due_at) == 10:  # "YYYY-MM-DD"
            due_at = due_at + "T00:00:00.000Z"
        body["due_at"] = due_at
    if site_id:
        body["site_id"] = _normalize_uuid(site_id)
    if collaborators:
        # Normalize collaborator IDs from feed format to clean UUIDs
        body["collaborators"] = [
            {**c, "collaborator_id": _normalize_uuid(c["collaborator_id"])}
            for c in collaborators
        ]

    async with _client() as client:
        resp = await client.post("/tasks/v1/actions", json=body)
        if resp.status_code != 200:
            logging.getLogger("sc_client").error(
                "SC create failed %s: %s — payload was: %s",
                resp.status_code, resp.text, body,
            )
        resp.raise_for_status()
        return resp.json()


async def update_action_status(action_id: str, status_id: str) -> dict:
    """Update action status."""
    async with _client() as client:
        resp = await client.put(
            f"/tasks/v1/actions/{action_id}/status",
            json={"status_id": status_id},
        )
        resp.raise_for_status()
        return resp.json()


async def update_action_title(action_id: str, title: str) -> dict:
    async with _client() as client:
        resp = await client.put(
            f"/tasks/v1/actions/{action_id}/title",
            json={"title": title},
        )
        resp.raise_for_status()
        return resp.json()


async def update_action_description(action_id: str, description: str) -> dict:
    async with _client() as client:
        resp = await client.put(
            f"/tasks/v1/actions/{action_id}/description",
            json={"description": description},
        )
        resp.raise_for_status()
        return resp.json()


async def update_action_priority(action_id: str, priority_id: str) -> dict:
    async with _client() as client:
        resp = await client.put(
            f"/tasks/v1/actions/{action_id}/priority",
            json={"priority_id": priority_id},
        )
        resp.raise_for_status()
        return resp.json()


async def update_action_due_date(action_id: str, due_at: str | None) -> dict:
    body = {}
    if due_at:
        if len(due_at) == 10:
            due_at = due_at + "T00:00:00.000Z"
        body["due_at"] = due_at
    async with _client() as client:
        resp = await client.put(
            f"/tasks/v1/actions/{action_id}/due_at",
            json=body,
        )
        resp.raise_for_status()
        return resp.json()


def _normalize_uuid(raw_id: str) -> str:
    """Convert a feed-format ID to a standard UUID string.

    Feed IDs may have a prefix (user_, location_, role_) and lack hyphens.
    The SC write APIs require standard 8-4-4-4-12 UUIDs.
    """
    for prefix in ("user_", "location_", "role_"):
        if raw_id.startswith(prefix):
            raw_id = raw_id[len(prefix):]
            break
    # If already contains hyphens, assume it's valid
    if "-" in raw_id:
        return raw_id
    # Re-insert hyphens: 8-4-4-4-12
    h = raw_id.replace("-", "")
    if len(h) == 32:
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
    return raw_id


async def update_action_assignees(action_id: str, user_ids: list[str]) -> dict:
    """Update action assignees. *user_ids* is a list of SC user IDs (feed format ok)."""
    assignees = [{
        "collaborator_id": _normalize_uuid(uid),
        "collaborator_type": "USER",
        "assigned_role": "ASSIGNEE",
    } for uid in user_ids]
    async with _client() as client:
        resp = await client.put(
            f"/tasks/v1/actions/{action_id}/assignees",
            json={"assignees": assignees},
        )
        if resp.status_code != 200:
            logging.getLogger("sc_client").error(
                "SC assignees update failed %s: %s", resp.status_code, resp.text,
            )
        resp.raise_for_status()
        return resp.json()


async def update_action_site(action_id: str, site_id: str) -> dict:
    """Update the site (location) of an action."""
    clean_id = _normalize_uuid(site_id)
    async with _client() as client:
        resp = await client.put(
            f"/tasks/v1/actions/{action_id}/site",
            json={"site_id": {"value": clean_id}},
        )
        if resp.status_code != 200:
            logging.getLogger("sc_client").error(
                "SC site update failed %s: %s", resp.status_code, resp.text,
            )
        resp.raise_for_status()
        return resp.json()


async def update_action_asset(action_id: str, asset_id: str) -> dict:
    """Update the asset linked to an action."""
    clean_id = _normalize_uuid(asset_id)
    async with _client() as client:
        resp = await client.put(
            f"/tasks/v1/actions/{action_id}/asset",
            json={"asset_id": {"value": clean_id}},
        )
        if resp.status_code != 200:
            logging.getLogger("sc_client").error(
                "SC asset update failed %s: %s", resp.status_code, resp.text,
            )
        resp.raise_for_status()
        return resp.json()


async def delete_actions(action_ids: list[str]) -> dict:
    """Delete actions in bulk."""
    async with _client() as client:
        resp = await client.post(
            "/tasks/v1/actions/delete",
            json={"ids": action_ids},
        )
        if resp.status_code != 200:
            logging.getLogger("sc_client").error(
                "SC bulk delete failed %s: %s", resp.status_code, resp.text,
            )
        resp.raise_for_status()
        return resp.json()


async def feed_actions(modified_after: str | None = None) -> dict:
    """Poll the actions feed for changes since a given timestamp."""
    params = {}
    if modified_after:
        params["modified_after"] = modified_after
    async with _client() as client:
        resp = await client.get("/feed/actions", params=params)
        resp.raise_for_status()
        return resp.json()


async def feed_sites(modified_after: str | None = None) -> dict:
    """Fetch all sites from the SC feed."""
    params = {}
    if modified_after:
        params["modified_after"] = modified_after
    async with _client() as client:
        resp = await client.get("/feed/sites", params=params)
        resp.raise_for_status()
        return resp.json()


async def feed_users(modified_after: str | None = None) -> dict:
    """Fetch all users from the SC feed."""
    params = {}
    if modified_after:
        params["modified_after"] = modified_after
    async with _client() as client:
        resp = await client.get("/feed/users", params=params)
        resp.raise_for_status()
        return resp.json()


async def feed_groups(modified_after: str | None = None) -> dict:
    """Fetch all groups from the SC feed."""
    params = {}
    if modified_after:
        params["modified_after"] = modified_after
    async with _client() as client:
        resp = await client.get("/feed/groups", params=params)
        resp.raise_for_status()
        return resp.json()


async def list_assets(page_size: int = 100, page_token: str | None = None) -> dict:
    """List assets from SC."""
    body: dict = {"page_size": page_size}
    if page_token:
        body["page_token"] = page_token
    async with _client() as client:
        resp = await client.post("/assets/v1/assets/list", json=body)
        resp.raise_for_status()
        return resp.json()


async def get_action_timeline(action_id: str) -> dict:
    """Get the full timeline (activity + comments) for an action."""
    async with _client() as client:
        resp = await client.post("/tasks/v1/timeline", json={"task_id": action_id})
        resp.raise_for_status()
        return resp.json()


async def add_action_comment(action_id: str, comment: str, event_id: str | None = None) -> dict:
    """Add a comment to an action's timeline.

    Pass event_id (a UUID) to make the call idempotent and so the resulting
    timeline item_id is predictable — allowing us to skip it during the next
    timeline sync and avoid duplicating the comment back into IssueTracker.
    """
    body: dict = {"task_id": action_id, "comment": comment}
    if event_id:
        body["event_id"] = event_id
    async with _client() as client:
        resp = await client.post("/tasks/v1/timeline/comments", json=body)
        resp.raise_for_status()
        return resp.json()


async def get_inspection_details(inspection_id: str) -> dict:
    """Fetch full inspection details including media URLs for all items."""
    async with _client() as client:
        resp = await client.get(
            f"/inspections/v1/inspections/{inspection_id}/details",
            params={"include_media_url": "true"},
        )
        resp.raise_for_status()
        return resp.json()


async def get_media_download_url(media_id: str, token: str) -> str | None:
    """Get a signed download URL for a SC media item.  Returns the URL string or None on error."""
    async with _client() as client:
        try:
            resp = await client.get(
                f"/media/v1/download/{media_id}",
                params={"token": token, "media_type": "MEDIA_TYPE_IMAGE"},
            )
            resp.raise_for_status()
            data = resp.json()
            # New API: download_info.url; legacy fallback: top-level url
            url = (
                (data.get("download_info") or {}).get("url")
                or data.get("url")
            )
            return url
        except Exception as e:
            logging.getLogger("sc_client").warning(
                "Failed to get download URL for media %s: %s", media_id, e
            )
            return None


async def get_inspection_media_bytes(inspection_id: str, media_id: str) -> tuple[bytes, str] | None:
    """Fetch raw media bytes for an inspection item.

    Returns (bytes, content_type) or None on error.
    """
    async with _client() as client:
        try:
            resp = await client.get(f"/audits/{inspection_id}/media/{media_id}")
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "image/jpeg")
            return resp.content, ct
        except Exception as e:
            logging.getLogger("sc_client").warning(
                "Failed to fetch inspection media %s/%s: %s", inspection_id, media_id, e
            )
            return None


async def upload_media_to_sc(
    file_bytes: bytes,
    filename: str,
    content_type: str = "image/jpeg",
) -> str | None:
    """Upload a file to SC media storage.  Returns the media_id on success or None.

    SC upload flow:
    1. Request a signed upload URL keyed by a client-generated UUID.
    2. PUT the binary to the S3 signed URL.
    The media_id is the UUID we generated; it can then be referenced in SC.
    """
    import uuid as _uuid
    media_id = str(_uuid.uuid4())
    async with _client() as client:
        try:
            # Step 1: obtain a signed upload URL
            resp = await client.get(
                f"/media/v1/upload/{media_id}",
                params={"media_type": "MEDIA_TYPE_IMAGE"},
            )
            resp.raise_for_status()
            data = resp.json()
            upload_url = (
                (data.get("upload_info") or {}).get("url")
                or data.get("url")
            )
            if not upload_url:
                logging.getLogger("sc_client").warning(
                    "No upload URL returned for media %s", media_id
                )
                return None

            # Step 2: PUT binary to S3 (no auth header — signed URL is pre-authenticated)
            async with httpx.AsyncClient(timeout=60.0) as s3:
                put_resp = await s3.put(
                    upload_url,
                    content=file_bytes,
                    headers={"Content-Type": content_type},
                )
                put_resp.raise_for_status()

            return media_id
        except Exception as e:
            logging.getLogger("sc_client").warning(
                "Failed to upload media to SC: %s", e
            )
            return None


async def attach_media_to_action(action_id: str, media_ids: list[str]) -> bool:
    """Attempt to attach uploaded media IDs to a SC action.

    This uses the timeline media endpoint (mirrors the comments endpoint).
    Returns True on success, False if the endpoint is unavailable.
    """
    if not media_ids:
        return True
    body = {"task_id": action_id, "media_ids": media_ids}
    async with _client() as client:
        try:
            resp = await client.post("/tasks/v1/timeline/media", json=body)
            if resp.status_code in (404, 405, 501):
                logging.getLogger("sc_client").warning(
                    "SC timeline/media endpoint not available (status %s) — "
                    "media uploaded to SC storage but not linked to action %s",
                    resp.status_code, action_id,
                )
                return False
            resp.raise_for_status()
            return True
        except Exception as e:
            logging.getLogger("sc_client").warning(
                "Failed to attach media to SC action %s: %s", action_id, e
            )
            return False
