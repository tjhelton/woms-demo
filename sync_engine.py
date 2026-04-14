"""Bidirectional sync engine between SafetyCulture Actions and IssueTracker."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta

import db
import sc_client

logger = logging.getLogger("sync_engine")

# Track the last time we polled SC for changes
_last_poll: str | None = None

# Track recently synced SC IDs with expiry timestamps to prevent echo loops.
# Maps sc_action_id → monotonic time when the suppression expires.
# The suppression window must survive at least one full poll cycle.
_recent_sc_syncs: dict[str, float] = {}
_ECHO_SUPPRESSION_SECONDS = 30  # suppress echoes for 30s after IT→SC sync

# Subscribers for real-time UI updates (SSE)
_subscribers: list[asyncio.Queue] = []


def _suppress_echo(sc_action_id: str):
    """Mark a SC action ID as recently synced from IssueTracker, suppressing
    feed-based echo updates for the suppression window."""
    _recent_sc_syncs[sc_action_id] = time.monotonic() + _ECHO_SUPPRESSION_SECONDS


def _is_echo_suppressed(sc_action_id: str) -> bool:
    """Check if a SC action ID is within the echo suppression window."""
    expiry = _recent_sc_syncs.get(sc_action_id)
    if expiry is None:
        return False
    if time.monotonic() < expiry:
        return True
    # Expired — clean up
    _recent_sc_syncs.pop(sc_action_id, None)
    return False


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _subscribers.append(q)
    return q


def unsubscribe(q: asyncio.Queue):
    if q in _subscribers:
        _subscribers.remove(q)


async def _broadcast(event: dict):
    """Push an event to all SSE subscribers."""
    for q in _subscribers:
        await q.put(event)


async def sync_sc_media_to_issuetracker(
    sc_action_id: str,
    wo_id: int,
    inspection_id: str = "",
    inspection_item_id: str = "",
    inspection_item_name: str = "",
    inspection_item_media: list | None = None,
) -> int:
    """Sync media from a SC action into the IssueTracker media table.

    Two sources are handled:
    1. Action-uploaded media (TASK_IMAGE_UPLOADED / TASK_MEDIA_UPLOADED timeline events)
       → labeled "Action Photo {n}"
    2. Inspection-question media (from the action's references[].inspection_context
       .inspection_item_attachments.media — the s12.common.Media items that have both
       id and token)
       → labeled "{question_label} {n}"

    inspection_item_media may be pre-fetched (from _merge_feed_and_detail) to avoid
    a redundant SC API call.  When None and inspection_id is set, the action detail
    is fetched to obtain it.

    Returns the count of newly-synced items.
    """
    synced = 0

    # --- Source 1: action timeline media ---
    try:
        timeline = await sc_client.get_action_timeline(sc_action_id)
    except Exception as e:
        logger.warning("Failed to fetch timeline for media sync on action %s: %s", sc_action_id, e)
        timeline = {}

    action_photo_index = 0
    for item in timeline.get("timeline_items", []):
        item_type = item.get("item_type", "")

        media_objects: list[dict] = []
        if item_type == "TASK_MEDIA_UPLOADED":
            data = item.get("task_media_uploaded_data") or {}
            media_objects = data.get("media") or []
        elif item_type == "TASK_IMAGE_UPLOADED":
            data = item.get("task_image_uploaded_data") or {}
            m = data.get("media")
            if m:
                media_objects = [m]

        for m in media_objects:
            sc_media_id = m.get("id")
            if not sc_media_id:
                continue
            if await db.media_exists_by_sc_id(sc_media_id):
                continue
            action_photo_index += 1
            await db.add_media(
                wo_id=wo_id,
                label=f"Action Photo {action_photo_index}",
                source="sc_action",
                sc_media_id=sc_media_id,
                sc_media_token=m.get("token"),
                media_type=_sc_media_type(m.get("media_type", "")),
            )
            synced += 1

    # --- Source 2: inspection item attachments ---
    # The action detail's references[].inspection_context.inspection_item_attachments
    # contains s12.common.Media objects (id + token) for the inspection item linked to
    # this action.  This is the correct data source — the old approach of fetching
    # inspection details and looking for item["attachments"]["media"] was wrong because
    # InspectionDetails.InspectionItem has no "attachments" property.
    if inspection_id:
        if inspection_item_media is None:
            # Not pre-fetched; pull from action detail references now.
            try:
                resp = await sc_client.get_action(sc_action_id)
                task = (resp.get("action") or {}).get("task") or {}
                for ref in task.get("references") or []:
                    ctx = ref.get("inspection_context") or {}
                    if ctx:
                        att = ctx.get("inspection_item_attachments") or {}
                        inspection_item_media = att.get("media") or []
                        if inspection_item_media:
                            break
            except Exception as e:
                logger.warning(
                    "Failed to fetch action detail for inspection media (action %s): %s",
                    sc_action_id, e,
                )
            if inspection_item_media is None:
                inspection_item_media = []

        question_label = inspection_item_name or "Inspection Photo"
        for idx, m in enumerate(inspection_item_media, start=1):
            sc_media_id = m.get("id")
            if not sc_media_id:
                continue
            if await db.media_exists_by_sc_id(sc_media_id):
                continue
            await db.add_media(
                wo_id=wo_id,
                label=f"{question_label} {idx}",
                source="sc_action",
                sc_media_id=sc_media_id,
                sc_media_token=m.get("token"),
                media_type=_sc_media_type(m.get("media_type", "")),
            )
            synced += 1

    if synced:
        logger.info(
            "Synced %d media items from SC action %s → WO %d", synced, sc_action_id, wo_id
        )
    return synced


def _sc_media_type(raw: str) -> str:
    """Normalize SC MediaType enum string to a short type label."""
    mapping = {
        "MEDIA_TYPE_IMAGE": "image",
        "MEDIA_TYPE_VIDEO": "video",
        "MEDIA_TYPE_PDF":   "pdf",
        "MEDIA_TYPE_DOCX":  "doc",
        "MEDIA_TYPE_XLSX":  "doc",
    }
    return mapping.get(raw, "image")


async def sync_sc_comments_to_issuetracker(sc_action_id: str, wo_id: int):
    """Pull SC action timeline and sync TASK_COMMENT_ADDED items to IssueTracker."""
    try:
        timeline = await sc_client.get_action_timeline(sc_action_id)
    except Exception as e:
        logger.warning("Failed to fetch SC timeline for %s: %s", sc_action_id, e)
        return

    items = timeline.get("timeline_items", [])
    synced = 0
    for item in items:
        if item.get("item_type") != "TASK_COMMENT_ADDED":
            continue
        item_id = item.get("item_id")
        if not item_id:
            continue
        if await db.comment_exists_by_sc_id(item_id):
            continue
        comment_data = item.get("task_comment_added_data", {})
        body = comment_data.get("comment", "")
        if not body:
            continue
        creator = item.get("creator") or {}
        author = (
            creator.get("name")
            or f"{creator.get('first_name', '')} {creator.get('last_name', '')}".strip()
            or "SafetyCulture"
        )
        await db.add_comment(
            wo_id=wo_id,
            author=author,
            body=body,
            sc_item_id=item_id,
            source="sc",
            created_at=item.get("timestamp"),
        )
        synced += 1

    if synced:
        logger.info("Synced %d comments from SC action %s → WO %d", synced, sc_action_id, wo_id)


async def _get_action_detail(action_id: str) -> dict | None:
    """Fetch full action detail from SC.  Returns the flattened task dict or None on error."""
    try:
        resp = await sc_client.get_action(action_id)
        task = resp.get("action", {}).get("task", {})
        return task if task else None
    except Exception as e:
        logger.warning("Failed to fetch action detail for %s: %s", action_id, e)
        return None


def _merge_feed_and_detail(feed: dict, detail: dict) -> dict:
    """Merge feed-format action with detail-format action.

    The detail endpoint has collaborators, richer site/asset info, and UUID-based
    status/priority.  The feed has human-readable status/priority strings.
    We keep both so the mapping code can handle either form.
    """
    merged = dict(feed)
    # Collaborators — only available from the detail
    merged["collaborators"] = detail.get("collaborators") or []
    # Status UUID — prefer the nested status object (current API) then top-level
    # status_id (deprecated for actions but still populated).  UUID lookup is
    # more reliable than matching feed string variants.
    status_obj = detail.get("status")
    status_uuid = (
        (status_obj.get("status_id") if isinstance(status_obj, dict) else None)
        or detail.get("status_id")
    )
    if status_uuid:
        merged["status_id"] = status_uuid
    if detail.get("priority_id"):
        merged["priority_id"] = detail["priority_id"]
    # Site — detail has a richer nested object with both id and name
    site = detail.get("site") or {}
    if site.get("id"):
        merged["site_id"] = site["id"]
    if site.get("name"):
        merged["site_name"] = site["name"]
    # Asset
    if detail.get("asset_id"):
        merged["asset_id"] = detail["asset_id"]
    # Due date — detail uses due_at
    if detail.get("due_at"):
        merged["due_at"] = detail["due_at"]
    # Creator
    creator = detail.get("creator") or {}
    if creator:
        name = f"{creator.get('firstname', '')} {creator.get('lastname', '')}".strip()
        if name:
            merged["creator_user_name"] = name
    # Inspection context — feed has audit_id/audit_item_id, detail has nested objects
    inspection = detail.get("inspection") or {}
    inspection_item = detail.get("inspection_item") or {}
    # Feed audit_id has format "audit_<32-char-hex>"; normalize to standard UUID
    raw_audit_id = feed.get("audit_id") or ""
    if raw_audit_id.startswith("audit_"):
        h = raw_audit_id[6:]
        if len(h) == 32:
            raw_audit_id = f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
    merged["inspection_id"] = inspection.get("inspection_id") or raw_audit_id or ""
    merged["inspection_name"] = inspection.get("inspection_name") or feed.get("audit_title") or ""
    merged["inspection_item_id"] = inspection_item.get("inspection_item_id") or feed.get("audit_item_id") or ""
    merged["inspection_item_name"] = inspection_item.get("inspection_item_name") or feed.get("audit_item_label") or ""
    # Pre-fetch inspection item media from references so sync_sc_media_to_issuetracker
    # doesn't need a second round-trip to the SC action detail endpoint.
    inspection_item_media: list[dict] = []
    for ref in detail.get("references") or []:
        ctx = ref.get("inspection_context") or {}
        if ctx:
            att = ctx.get("inspection_item_attachments") or {}
            inspection_item_media = att.get("media") or []
            if inspection_item_media:
                break
    merged["inspection_item_media"] = inspection_item_media
    return merged


async def sync_sc_action_to_issuetracker(action: dict) -> dict | None:
    """Given a SC action (from feed or API), create or update the IssueTracker work order."""
    action_id = action.get("id") or action.get("task_id")
    if not action_id:
        return None

    if _is_echo_suppressed(action_id):
        logger.debug("Skipping echo for SC action %s (recently synced from IT)", action_id)
        return None

    # Skip actions that the user intentionally deleted
    if await db.is_sc_action_deleted(action_id):
        logger.debug("Skipping deleted SC action %s", action_id)
        return None

    # The feed only provides a flat summary without collaborators, site
    # details, or asset info.  Fetch the full action so we always have
    # the richest data available.
    detail = await _get_action_detail(action_id)
    if detail:
        action = _merge_feed_and_detail(action, detail)

    title = action.get("title", "Untitled Action")
    description = action.get("description", "")
    # Prefer UUID (reliable, set from detail fetch) over the feed string which
    # can vary in format across SC API versions (e.g. "IN_PROGRESS" vs "IN PROGRESS").
    status_raw = action.get("status_id") or action.get("status", "")
    priority_raw = action.get("priority_id") or action.get("priority", "0")
    due_date = action.get("due_date") or action.get("due_at")
    creator = action.get("creator_user_name", "")
    site_id = action.get("site_id", "")
    site_name = action.get("site_name", "")
    if site_id and not site_name:
        site_name = await db.get_sc_site_name(site_id) or ""
    inspection_id = action.get("inspection_id", "")
    inspection_name = action.get("inspection_name", "")
    inspection_item_id = action.get("inspection_item_id", "")
    inspection_item_name = action.get("inspection_item_name", "")

    # Resolve assignee from collaborators (populated by the detail fetch)
    assignee = ""
    collaborators = action.get("collaborators") or []
    for collab in collaborators:
        if collab.get("assigned_role") == "ASSIGNEE":
            user = collab.get("user") or {}
            name = f"{user.get('firstname', '')} {user.get('lastname', '')}".strip()
            if name:
                assignee = name
                break
    # Fallback: first collaborator of any role, then creator
    if not assignee:
        for collab in collaborators:
            user = collab.get("user") or {}
            name = f"{user.get('firstname', '')} {user.get('lastname', '')}".strip()
            if name:
                assignee = name
                break
    if not assignee:
        assignee = creator

    # Resolve asset code from action
    asset_id = action.get("asset_id", "")
    asset_code = ""
    if asset_id:
        asset_code = await db.get_sc_asset_code(asset_id) or asset_id

    it_status = db.SC_STATUS_TO_ISSUETRACKER.get(status_raw, "Open")
    it_priority = db.SC_PRIORITY_TO_ISSUETRACKER.get(str(priority_raw), "None")

    existing = await db.get_work_order_by_sc_id(action_id)

    if existing:
        changes = []
        updates = {}
        if existing["title"] != title:
            updates["title"] = title
            changes.append(f"title → '{title}'")
        if existing["description"] != description:
            updates["description"] = description
            changes.append("description updated")
        if existing["status"] != it_status:
            updates["status"] = it_status
            changes.append(f"status → {it_status}")
        if existing["priority"] != it_priority:
            updates["priority"] = it_priority
            changes.append(f"priority → {it_priority}")
        if due_date and existing.get("due_date") != due_date:
            updates["due_date"] = due_date
            changes.append(f"due date → {due_date}")
        if assignee and existing.get("assignee") != assignee:
            updates["assignee"] = assignee
            changes.append(f"assignee → {assignee}")
        if site_name and existing.get("location") != site_name:
            updates["location"] = site_name
            changes.append(f"location → {site_name}")
        if asset_code and existing.get("asset") != asset_code:
            updates["asset"] = asset_code
            changes.append(f"asset → {asset_code}")

        # Update metadata with inspection context if it has changed
        existing_meta = json.loads(existing.get("metadata") or "{}")
        new_meta = dict(existing_meta)
        if inspection_id:
            new_meta["inspection_id"] = inspection_id
            new_meta["inspection_name"] = inspection_name
            new_meta["inspection_item_id"] = inspection_item_id
            new_meta["inspection_item_name"] = inspection_item_name
        if new_meta != existing_meta:
            updates["metadata"] = json.dumps(new_meta)

        if updates:
            updates["sc_last_synced"] = datetime.now(timezone.utc).isoformat()
            wo = await db.update_work_order(existing["id"], **updates)
            detail = "; ".join(changes)
            await db.add_sync_log(
                direction="SC → IssueTracker",
                event_type="update",
                sc_action_id=action_id,
                wo_number=existing["wo_number"],
                details=detail,
            )
            await _broadcast({
                "type": "sync",
                "direction": "SC → IssueTracker",
                "event": "update",
                "wo_number": existing["wo_number"],
                "sc_action_id": action_id,
                "details": detail,
            })
            logger.info("Updated WO %s from SC action %s: %s", existing["wo_number"], action_id, detail)
        else:
            wo = existing

        # Sync comments from SC timeline
        try:
            await sync_sc_comments_to_issuetracker(action_id, existing["id"])
        except Exception as e:
            logger.warning("Comment sync failed for SC action %s: %s", action_id, e)

        # Sync media from SC action and linked inspection item
        try:
            await sync_sc_media_to_issuetracker(
                action_id, existing["id"],
                inspection_id=inspection_id,
                inspection_item_id=inspection_item_id,
                inspection_item_name=inspection_item_name,
                inspection_item_media=action.get("inspection_item_media"),
            )
        except Exception as e:
            logger.warning("Media sync failed for SC action %s: %s", action_id, e)

        return wo
    else:
        meta: dict = {}
        if inspection_id:
            meta["inspection_id"] = inspection_id
            meta["inspection_name"] = inspection_name
            meta["inspection_item_id"] = inspection_item_id
            meta["inspection_item_name"] = inspection_item_name
        wo = await db.create_work_order(
            title=title,
            description=description,
            status=it_status,
            priority=it_priority,
            assignee=assignee,
            location=site_name,
            asset=asset_code,
            due_date=due_date,
            sc_action_id=action_id,
            metadata=meta or None,
        )
        await db.add_sync_log(
            direction="SC → IssueTracker",
            event_type="create",
            sc_action_id=action_id,
            wo_number=wo["wo_number"],
            details=f"Created from SC action: {title}",
        )
        await _broadcast({
            "type": "sync",
            "direction": "SC → IssueTracker",
            "event": "create",
            "wo_number": wo["wo_number"],
            "sc_action_id": action_id,
            "details": f"Created work order from SC action: {title}",
        })
        logger.info("Created WO %s from SC action %s", wo["wo_number"], action_id)

        # Sync comments from SC timeline for new WO
        try:
            await sync_sc_comments_to_issuetracker(action_id, wo["id"])
        except Exception as e:
            logger.warning("Comment sync failed for new WO from SC action %s: %s", action_id, e)

        # Sync media for new WO
        try:
            await sync_sc_media_to_issuetracker(
                action_id, wo["id"],
                inspection_id=inspection_id,
                inspection_item_id=inspection_item_id,
                inspection_item_name=inspection_item_name,
                inspection_item_media=action.get("inspection_item_media"),
            )
        except Exception as e:
            logger.warning("Media sync failed for new WO from SC action %s: %s", action_id, e)

        return wo


async def sync_issuetracker_to_sc(wo: dict, changed_fields: dict) -> bool:
    """Push IssueTracker work order changes back to SafetyCulture.

    Only the fields present in *changed_fields* are synced — pass exactly the
    dict of fields that were modified so no redundant SC API calls are made.
    """
    sc_action_id = wo.get("sc_action_id")
    if not sc_action_id:
        return False

    _suppress_echo(sc_action_id)
    try:
        changes = []

        if "status" in changed_fields:
            expected_sc_status = db.ISSUETRACKER_STATUS_TO_SC.get(wo["status"])
            if expected_sc_status:
                try:
                    await sc_client.update_action_status(sc_action_id, expected_sc_status)
                    changes.append(f"status → {wo['status']}")
                except Exception as e:
                    logger.error("Failed to update SC status: %s", e)

        if "title" in changed_fields:
            try:
                await sc_client.update_action_title(sc_action_id, wo["title"])
                changes.append(f"title → '{wo['title']}'")
            except Exception as e:
                logger.error("Failed to update SC title: %s", e)

        if "priority" in changed_fields:
            expected_sc_priority = db.ISSUETRACKER_PRIORITY_TO_SC.get(wo["priority"])
            if expected_sc_priority:
                try:
                    await sc_client.update_action_priority(sc_action_id, expected_sc_priority)
                    changes.append(f"priority → {wo['priority']}")
                except Exception as e:
                    logger.error("Failed to update SC priority: %s", e)

        if "description" in changed_fields:
            try:
                await sc_client.update_action_description(sc_action_id, wo.get("description") or "")
                changes.append("description updated")
            except Exception as e:
                logger.error("Failed to update SC description: %s", e)

        if "due_date" in changed_fields:
            wo_due = wo.get("due_date")
            if wo_due:
                try:
                    await sc_client.update_action_due_date(sc_action_id, wo_due)
                    changes.append(f"due date → {wo_due[:10]}")
                except Exception as e:
                    logger.error("Failed to update SC due date: %s", e)

        if "assignee" in changed_fields:
            assignee = wo.get("assignee", "").strip()
            if assignee:
                user_id = await db.get_sc_user_id_by_name(assignee)
                if user_id:
                    try:
                        await sc_client.update_action_assignees(sc_action_id, [user_id])
                        changes.append(f"assignee → {assignee}")
                    except Exception as e:
                        logger.error("Failed to update SC assignees: %s", e)
                else:
                    logger.warning("Could not resolve assignee '%s' to a SC user", assignee)

        if "location" in changed_fields:
            location = wo.get("location", "").strip()
            if location:
                site_id = await db.get_sc_site_id_by_name(location)
                if site_id:
                    try:
                        await sc_client.update_action_site(sc_action_id, site_id)
                        changes.append(f"site → {location}")
                    except Exception as e:
                        logger.error("Failed to update SC site: %s", e)
                else:
                    logger.warning("Could not resolve location '%s' to a SC site", location)

        if "asset" in changed_fields:
            asset = wo.get("asset", "").strip()
            if asset:
                asset_id = await db.get_sc_asset_id_by_code(asset)
                if asset_id:
                    try:
                        await sc_client.update_action_asset(sc_action_id, asset_id)
                        changes.append(f"asset → {asset}")
                    except Exception as e:
                        logger.error("Failed to update SC asset: %s", e)
                else:
                    logger.warning("Could not resolve asset '%s' to a SC asset", asset)

        if changes:
            detail = "; ".join(changes)
            await db.update_work_order(wo["id"], sc_last_synced=datetime.now(timezone.utc).isoformat())
            await db.add_sync_log(
                direction="IssueTracker → SC",
                event_type="update",
                sc_action_id=sc_action_id,
                wo_number=wo["wo_number"],
                details=detail,
            )
            await _broadcast({
                "type": "sync",
                "direction": "IssueTracker → SC",
                "event": "update",
                "wo_number": wo["wo_number"],
                "sc_action_id": sc_action_id,
                "details": detail,
            })
            logger.info("Synced WO %s → SC action %s: %s", wo["wo_number"], sc_action_id, detail)
            # Refresh the suppression window after the sync completes
            _suppress_echo(sc_action_id)
            return True
        return False
    except Exception:
        logger.exception("Unexpected error during IT→SC sync for %s", sc_action_id)
        return False


async def backfill_inspection_metadata():
    """One-shot startup task: for every linked WO that has no inspection_id in
    its metadata, fetch the SC action detail and populate the inspection fields.
    Handles work orders synced before inspection extraction was added."""
    tracked = await db.get_tracked_sc_actions()
    if not tracked:
        return

    filled = 0
    for row in tracked:
        wo_id = row["id"]
        sc_id = row["sc_action_id"]

        # Read full WO to check metadata
        wo = await db.get_work_order(wo_id)
        if not wo:
            continue
        meta = json.loads(wo.get("metadata") or "{}")
        if meta.get("inspection_id"):
            continue  # already populated

        task = await _get_action_detail(sc_id)
        if not task:
            continue

        inspection = task.get("inspection") or {}
        inspection_item = task.get("inspection_item") or {}
        inspection_id = inspection.get("inspection_id", "")
        if not inspection_id:
            continue  # no inspection linked to this action

        meta["inspection_id"] = inspection_id
        meta["inspection_name"] = inspection.get("inspection_name", "")
        meta["inspection_item_id"] = inspection_item.get("inspection_item_id", "")
        meta["inspection_item_name"] = inspection_item.get("inspection_item_name", "")

        await db.update_work_order(wo_id, metadata=json.dumps(meta))
        filled += 1
        logger.info(
            "Backfilled inspection metadata for WO %s (SC %s): %s",
            wo["wo_number"], sc_id, inspection_id,
        )

    if filled:
        logger.info("Inspection metadata backfill complete: %d work order(s) updated", filled)


async def reconcile_deleted_actions():
    """Detect SC actions that have been deleted and remove the corresponding
    IssueTracker work orders.  The SC feed never reports deletions, so we
    periodically check whether each tracked action still exists."""
    tracked = await db.get_tracked_sc_actions()
    if not tracked:
        return

    for wo in tracked:
        sc_id = wo["sc_action_id"]
        # Skip actions we already know are deleted
        if await db.is_sc_action_deleted(sc_id):
            continue
        try:
            exists = await sc_client.action_exists(sc_id)
        except Exception as e:
            logger.warning("Failed to check SC action %s: %s", sc_id, e)
            continue

        if not exists:
            logger.info(
                "SC action %s no longer exists — deleting WO %s",
                sc_id, wo["wo_number"],
            )
            await db.mark_sc_action_deleted(sc_id)
            await db.delete_work_order(wo["id"])
            await db.add_sync_log(
                direction="SC → IssueTracker",
                event_type="delete",
                sc_action_id=sc_id,
                wo_number=wo["wo_number"],
                details=f"SC action deleted — removed work order {wo['wo_number']}",
            )
            await _broadcast({
                "type": "wo_delete",
                "wo_number": wo["wo_number"],
                "details": "Deleted in SafetyCulture — work order removed",
            })


async def poll_sc_feed():
    """Poll SafetyCulture feed for new/changed actions and sync to IssueTracker."""
    global _last_poll

    # Record the poll timestamp *before* the request so we don't miss changes
    # that happen while the request is in flight.
    poll_ts = datetime.now(timezone.utc).isoformat()

    try:
        feed = await sc_client.feed_actions(modified_after=_last_poll)
    except Exception as e:
        logger.error("Failed to poll SC feed: %s", e)
        await _broadcast({"type": "error", "details": f"SC feed poll failed: {e}"})
        return

    _last_poll = poll_ts

    # Prune expired echo-suppression entries
    now_mono = time.monotonic()
    expired = [k for k, v in _recent_sc_syncs.items() if now_mono >= v]
    for k in expired:
        _recent_sc_syncs.pop(k, None)

    data = feed.get("data", [])
    if data:
        logger.info("Feed returned %d actions", len(data))
        for action in data:
            try:
                await sync_sc_action_to_issuetracker(action)
            except Exception as e:
                logger.error("Error syncing action %s: %s", action.get("id"), e)


async def run_sync_loop():
    """Background task: continuously poll SC and sync."""
    interval = int(os.getenv("SYNC_INTERVAL_SECONDS", "10"))
    logger.info("Sync loop started (interval=%ds)", interval)

    # Initial poll goes back 24h to pick up recent actions
    global _last_poll
    _last_poll = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    # Run deletion reconciliation every RECONCILE_EVERY polls (~once per minute)
    _RECONCILE_EVERY = 6
    poll_count = 0

    while True:
        await poll_sc_feed()
        poll_count += 1

        if poll_count % _RECONCILE_EVERY == 0:
            try:
                await reconcile_deleted_actions()
            except Exception as e:
                logger.error("Reconciliation failed: %s", e)

        await asyncio.sleep(interval)
