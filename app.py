"""FastAPI application — serves the dashboard and API endpoints."""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pathlib import Path

import uuid

import db
import sc_client
import sync_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    logger.info("Database initialized")

    # Start the sync loop if we have an API token configured
    token = os.getenv("SC_API_TOKEN", "")
    if token:
        # Detect token change and reset data so a new SC account starts clean
        if await db.check_token_and_reset(token):
            logger.info("Database reset for new SC account")
        sync_task = asyncio.create_task(sync_engine.run_sync_loop())
        logger.info("SC sync loop started")
        # Kick off initial objects sync and inspection metadata backfill in the background
        asyncio.create_task(_sync_sc_objects())
        asyncio.create_task(sync_engine.backfill_inspection_metadata())
    else:
        sync_task = None
        logger.warning("SC_API_TOKEN not set — running in demo-only mode (no live sync)")

    yield

    if sync_task:
        sync_task.cancel()
        try:
            await sync_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="WOMS — Work Order Management System", lifespan=lifespan)

# Serve static files
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
uploads_dir = static_dir / "uploads"
uploads_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html_path = Path(__file__).parent / "static" / "index.html"
    return HTMLResponse(html_path.read_text())


# ---------------------------------------------------------------------------
# SSE endpoint for live updates
# ---------------------------------------------------------------------------

@app.get("/api/events")
async def sse_events(request: Request):
    queue = sync_engine.subscribe()

    async def event_stream():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    # Send keepalive comment to detect broken connections
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            sync_engine.unsubscribe(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# IssueTracker Work Order API
# ---------------------------------------------------------------------------

@app.get("/api/work-orders")
async def list_work_orders():
    orders = await db.list_work_orders()
    return {"work_orders": orders}


@app.get("/api/work-orders/{wo_id}")
async def get_work_order(wo_id: int):
    wo = await db.get_work_order(wo_id)
    if not wo:
        raise HTTPException(404, "Work order not found")
    return wo


@app.post("/api/work-orders")
async def create_work_order(request: Request):
    body = await request.json()
    title = body.get("title")
    if not title:
        raise HTTPException(400, "title is required")

    wo = await db.create_work_order(
        title=title,
        description=body.get("description", ""),
        status=body.get("status", "Open"),
        priority=body.get("priority", "None"),
        assignee=body.get("assignee", ""),
        location=body.get("location", ""),
        asset=body.get("asset", ""),
        due_date=body.get("due_date"),
    )

    # If sync is active, also create in SafetyCulture
    token = os.getenv("SC_API_TOKEN", "")
    if token:
        try:
            sc_priority = db.ISSUETRACKER_PRIORITY_TO_SC.get(wo["priority"])
            sc_status = db.ISSUETRACKER_STATUS_TO_SC.get(wo["status"])

            # Resolve assignee → SC collaborator
            sc_collaborators = None
            assignee_name = wo.get("assignee", "").strip()
            if assignee_name:
                user_id = await db.get_sc_user_id_by_name(assignee_name)
                if user_id:
                    sc_collaborators = [{
                        "collaborator_id": user_id,
                        "collaborator_type": "USER",
                        "assigned_role": "ASSIGNEE",
                    }]

            # Resolve location → SC site
            sc_site_id = None
            location_name = wo.get("location", "").strip()
            if location_name:
                sc_site_id = await db.get_sc_site_id_by_name(location_name)

            sc_resp = await sc_client.create_action(
                title=wo["title"],
                description=wo["description"],
                priority_id=sc_priority,
                status_id=sc_status,
                due_at=wo.get("due_date"),
                site_id=sc_site_id,
                collaborators=sc_collaborators,
            )
            sc_action_id = sc_resp.get("action_id") or sc_resp.get("task_id") or sc_resp.get("id")
            if sc_action_id:
                now = datetime.now(timezone.utc).isoformat()
                wo = await db.update_work_order(wo["id"], sc_action_id=sc_action_id, sc_last_synced=now)
                sync_engine._suppress_echo(sc_action_id)

                # Set asset after creation (no create-time API for it)
                asset_code = wo.get("asset", "").strip()
                if asset_code:
                    asset_id = await db.get_sc_asset_id_by_code(asset_code)
                    if asset_id:
                        try:
                            await sc_client.update_action_asset(sc_action_id, asset_id)
                        except Exception:
                            logger.warning("Failed to set asset on new SC action %s", sc_action_id)
                await db.add_sync_log(
                    direction="IssueTracker → SC",
                    event_type="create",
                    sc_action_id=sc_action_id,
                    wo_number=wo["wo_number"],
                    details=f"Created SC action from work order: {title}",
                )
                await sync_engine._broadcast({
                    "type": "sync",
                    "direction": "IssueTracker → SC",
                    "event": "create",
                    "wo_number": wo["wo_number"],
                    "sc_action_id": sc_action_id,
                    "details": f"Created SC action from work order: {title}",
                })
        except Exception as e:
            logger.error("Failed to create SC action: %s", e)
            await db.add_sync_log(
                direction="IssueTracker → SC",
                event_type="create",
                wo_number=wo["wo_number"],
                details=f"Failed to create SC action: {e}",
                status="error",
            )

    return wo


@app.put("/api/work-orders/{wo_id}")
async def update_work_order(wo_id: int, request: Request):
    existing = await db.get_work_order(wo_id)
    if not existing:
        raise HTTPException(404, "Work order not found")

    body = await request.json()
    allowed = {"title", "description", "status", "priority", "assignee", "location", "asset", "due_date"}
    updates = {k: v for k, v in body.items() if k in allowed}

    if not updates:
        return existing

    # Normalize None → "" for text fields to match DB defaults and avoid
    # false-positive change detection in the sync engine.
    text_fields = {"title", "description", "assignee", "location", "asset"}
    for k in text_fields:
        if k in updates and updates[k] is None:
            updates[k] = ""

    wo = await db.update_work_order(wo_id, **updates)

    # Sync changes to SafetyCulture if linked
    if wo.get("sc_action_id"):
        token = os.getenv("SC_API_TOKEN", "")
        if token:
            try:
                await sync_engine.sync_issuetracker_to_sc(wo, changed_fields=updates)
                # Re-read to include sc_last_synced set by the sync engine
                wo = await db.get_work_order(wo_id) or wo
            except Exception as e:
                logger.error("Failed to sync to SC: %s", e)

    await sync_engine._broadcast({
        "type": "wo_update",
        "wo_number": wo["wo_number"],
        "updates": updates,
    })

    return wo


@app.delete("/api/work-orders/{wo_id}")
async def delete_work_order(wo_id: int):
    existing = await db.get_work_order(wo_id)
    if not existing:
        raise HTTPException(404, "Work order not found")

    await db.delete_work_order(wo_id)

    # Delete linked SC action so the sync engine doesn't recreate the work order
    sc_action_id = existing.get("sc_action_id")
    if sc_action_id:
        await db.mark_sc_action_deleted(sc_action_id)
        token = os.getenv("SC_API_TOKEN", "")
        if token:
            try:
                await sc_client.delete_actions([sc_action_id])
                await db.add_sync_log(
                    direction="IssueTracker → SC",
                    event_type="delete",
                    sc_action_id=sc_action_id,
                    wo_number=existing["wo_number"],
                    details=f"Deleted SC action for: {existing['wo_number']}",
                )
            except Exception as e:
                logger.error("Failed to delete SC action %s: %s", sc_action_id, e)

    await sync_engine._broadcast({
        "type": "wo_delete",
        "wo_number": existing["wo_number"],
    })

    return {"status": "ok", "wo_number": existing["wo_number"]}


@app.post("/api/work-orders/bulk-delete")
async def bulk_delete_work_orders(request: Request):
    body = await request.json()
    wo_ids = body.get("ids", [])
    if not wo_ids:
        raise HTTPException(400, "ids is required")

    deleted = await db.bulk_delete_work_orders(wo_ids)

    # Mark linked SC actions as deleted so the sync engine won't recreate them
    sc_ids = [d["sc_action_id"] for d in deleted if d.get("sc_action_id")]
    for sc_id in sc_ids:
        await db.mark_sc_action_deleted(sc_id)
    if sc_ids:
        token = os.getenv("SC_API_TOKEN", "")
        if token:
            # Delete from SC one at a time — the bulk endpoint can fail
            # silently for large batches, while individual deletes are reliable.
            for d in deleted:
                sc_id = d.get("sc_action_id")
                if not sc_id:
                    continue
                try:
                    await sc_client.delete_actions([sc_id])
                    await db.add_sync_log(
                        direction="IssueTracker → SC",
                        event_type="delete",
                        sc_action_id=sc_id,
                        wo_number=d["wo_number"],
                        details=f"Bulk deleted: {d['wo_number']}",
                    )
                except Exception as e:
                    logger.error("Failed to delete SC action %s: %s", sc_id, e)

    wo_numbers = [d["wo_number"] for d in deleted]
    await sync_engine._broadcast({
        "type": "wo_bulk_delete",
        "wo_numbers": wo_numbers,
    })

    return {"status": "ok", "deleted": wo_numbers}


# ---------------------------------------------------------------------------
# Work Order Comments
# ---------------------------------------------------------------------------

@app.get("/api/work-orders/{wo_id}/comments")
async def get_wo_comments(wo_id: int):
    wo = await db.get_work_order(wo_id)
    if not wo:
        raise HTTPException(404, "Work order not found")

    # Pull fresh comments from SC on every view — catches comments added
    # directly in SafetyCulture that may not have triggered a feed update.
    sc_action_id = wo.get("sc_action_id")
    if sc_action_id:
        token = os.getenv("SC_API_TOKEN", "")
        if token:
            try:
                await sync_engine.sync_sc_comments_to_issuetracker(sc_action_id, wo_id)
            except Exception as e:
                logger.warning("On-demand comment sync failed for WO %s: %s", wo_id, e)

    comments = await db.get_comments(wo_id)
    return {"comments": comments}


@app.post("/api/work-orders/{wo_id}/comments")
async def add_wo_comment(wo_id: int, request: Request):
    wo = await db.get_work_order(wo_id)
    if not wo:
        raise HTTPException(404, "Work order not found")

    body = await request.json()
    text = (body.get("body") or "").strip()
    author = (body.get("author") or "User").strip()

    if not text:
        raise HTTPException(400, "body is required")

    # Generate a stable event_id so this comment won't be re-imported from SC
    event_id = str(uuid.uuid4())
    comment = await db.add_comment(
        wo_id=wo_id, author=author, body=text,
        source="issuetracker", sc_item_id=event_id,
    )

    # Push to SC if linked and token is configured
    sc_action_id = wo.get("sc_action_id")
    if sc_action_id:
        token = os.getenv("SC_API_TOKEN", "")
        if token:
            try:
                await sc_client.add_action_comment(sc_action_id, text, event_id=event_id)
                await db.add_sync_log(
                    direction="IssueTracker → SC",
                    event_type="comment",
                    sc_action_id=sc_action_id,
                    wo_number=wo["wo_number"],
                    details=f"Comment synced to SC: {text[:80]}",
                )
                await sync_engine._broadcast({
                    "type": "sync",
                    "direction": "IssueTracker → SC",
                    "event": "comment",
                    "wo_number": wo["wo_number"],
                    "sc_action_id": sc_action_id,
                    "details": "Comment posted and synced to SafetyCulture",
                })
            except Exception as e:
                logger.error("Failed to push comment to SC: %s", e)

    return comment


# ---------------------------------------------------------------------------
# SafetyCulture proxy endpoints (for the dashboard to call)
# ---------------------------------------------------------------------------

@app.get("/api/sc/actions")
async def list_sc_actions():
    """Proxy to list SC actions."""
    token = os.getenv("SC_API_TOKEN", "")
    if not token:
        return {"actions": [], "demo_mode": True}
    try:
        resp = await sc_client.list_actions(page_size=50)
        raw_actions = resp.get("actions", [])
        # SC list endpoint wraps each action in {task: {...}}; flatten for consumers
        actions = []
        for item in raw_actions:
            task = item.get("task", item) if isinstance(item, dict) else item
            actions.append(task)
        return {"actions": actions}
    except Exception as e:
        logger.error("Failed to list SC actions: %s", e)
        return {"actions": [], "error": str(e)}


@app.post("/api/sc/actions")
async def create_sc_action(request: Request):
    """Create an action in SC and sync to IssueTracker."""
    token = os.getenv("SC_API_TOKEN", "")
    if not token:
        raise HTTPException(503, "SC API not configured")

    body = await request.json()
    title = body.get("title")
    if not title:
        raise HTTPException(400, "title is required")

    sc_resp = await sc_client.create_action(
        title=title,
        description=body.get("description", ""),
        priority_id=body.get("priority_id"),
        status_id=body.get("status_id"),
        due_at=body.get("due_at"),
    )

    sc_action_id = sc_resp.get("action_id") or sc_resp.get("task_id") or sc_resp.get("id")

    # Create corresponding IssueTracker work order
    if sc_action_id:
        sync_engine._suppress_echo(sc_action_id)
        wo = await db.create_work_order(
            title=title,
            description=body.get("description", ""),
            status=db.SC_STATUS_TO_ISSUETRACKER.get(body.get("status_id", ""), "Open"),
            priority=db.SC_PRIORITY_TO_ISSUETRACKER.get(body.get("priority_id", ""), "None"),
            sc_action_id=sc_action_id,
        )
        await db.add_sync_log(
            direction="SC → IssueTracker",
            event_type="create",
            sc_action_id=sc_action_id,
            wo_number=wo["wo_number"],
            details=f"Created from new SC action: {title}",
        )
        await sync_engine._broadcast({
            "type": "sync",
            "direction": "SC → IssueTracker",
            "event": "create",
            "wo_number": wo["wo_number"],
            "sc_action_id": sc_action_id,
            "details": f"Action created in SC, work order created in IssueTracker: {title}",
        })

    return sc_resp


# ---------------------------------------------------------------------------
# SC Objects (reference data: sites, users, groups, assets)
# ---------------------------------------------------------------------------

async def _sync_sc_objects():
    """Fetch sites, users, groups, assets from SC and cache locally."""
    token = os.getenv("SC_API_TOKEN", "")
    if not token:
        return {"error": "SC API not configured"}

    results = {}

    try:
        resp = await sc_client.feed_sites()
        sites = resp.get("data", [])
        if sites:
            await db.upsert_sc_sites(sites)
        results["sites"] = len(sites)
    except Exception as e:
        logger.error("Failed to sync SC sites: %s", e)
        results["sites_error"] = str(e)

    try:
        resp = await sc_client.feed_users()
        users = resp.get("data", [])
        if users:
            await db.upsert_sc_users(users)
        results["users"] = len(users)
    except Exception as e:
        logger.error("Failed to sync SC users: %s", e)
        results["users_error"] = str(e)

    try:
        resp = await sc_client.feed_groups()
        groups = resp.get("data", [])
        if groups:
            await db.upsert_sc_groups(groups)
        results["groups"] = len(groups)
    except Exception as e:
        logger.error("Failed to sync SC groups: %s", e)
        results["groups_error"] = str(e)

    try:
        assets_all = []
        page_token = None
        while True:
            resp = await sc_client.list_assets(page_size=100, page_token=page_token)
            batch = resp.get("assets", [])
            assets_all.extend(batch)
            page_token = resp.get("next_page_token")
            if not page_token or not batch:
                break
        await db.upsert_sc_assets(assets_all)
        results["assets"] = len(assets_all)
    except Exception as e:
        logger.error("Failed to sync SC assets: %s", e)
        results["assets_error"] = str(e)

    logger.info("SC objects synced: %s", results)
    return results


@app.get("/api/sc/objects")
async def get_sc_objects():
    """Return cached SC reference objects (sites, users, groups, assets)."""
    objects = await db.get_sc_objects()
    return objects


@app.post("/api/sc/sync-objects")
async def sync_sc_objects():
    """Trigger a fresh fetch of SC reference objects."""
    results = await _sync_sc_objects()
    return {"status": "ok", "results": results}


# ---------------------------------------------------------------------------
# Sync log
# ---------------------------------------------------------------------------

@app.get("/api/sync-log")
async def get_sync_log(limit: int = 50):
    logs = await db.get_sync_logs(limit)
    return {"logs": logs}


# ---------------------------------------------------------------------------
# Manual sync trigger
# ---------------------------------------------------------------------------

@app.post("/api/sync/trigger")
async def trigger_sync():
    """Manually trigger a sync poll."""
    await sync_engine.poll_sc_feed()
    return {"status": "ok", "message": "Sync triggered"}


# ---------------------------------------------------------------------------
# Media — per-work-order and global gallery
# ---------------------------------------------------------------------------

_ALLOWED_CONTENT_TYPES = {
    "image/jpeg", "image/png", "image/gif", "image/webp",
    "image/heic", "image/heif",
    "video/mp4", "video/quicktime",
    "application/pdf",
}

_CONTENT_TYPE_TO_MEDIA_TYPE = {
    "image/jpeg": "image", "image/png": "image", "image/gif": "image",
    "image/webp": "image", "image/heic": "image", "image/heif": "image",
    "video/mp4": "video", "video/quicktime": "video",
    "application/pdf": "pdf",
}


@app.get("/api/work-orders/{wo_id}/media")
async def get_wo_media(wo_id: int):
    """Return all media for a work order, triggering an on-demand SC sync first."""
    wo = await db.get_work_order(wo_id)
    if not wo:
        raise HTTPException(404, "Work order not found")

    sc_action_id = wo.get("sc_action_id")
    if sc_action_id:
        token = os.getenv("SC_API_TOKEN", "")
        if token:
            meta = json.loads(wo.get("metadata") or "{}")
            try:
                await sync_engine.sync_sc_media_to_issuetracker(
                    sc_action_id, wo_id,
                    inspection_id=meta.get("inspection_id", ""),
                    inspection_item_id=meta.get("inspection_item_id", ""),
                    inspection_item_name=meta.get("inspection_item_name", ""),
                )
            except Exception as e:
                logger.warning("On-demand media sync failed for WO %s: %s", wo_id, e)

    media = await db.get_media(wo_id)
    return {"media": media}


@app.post("/api/work-orders/{wo_id}/media")
async def upload_wo_media(wo_id: int, file: UploadFile = File(...)):
    """Upload a media file to a work order and optionally push it to SC."""
    wo = await db.get_work_order(wo_id)
    if not wo:
        raise HTTPException(404, "Work order not found")

    content_type = file.content_type or "image/jpeg"
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(400, f"Unsupported file type: {content_type}")

    # Save locally
    file_bytes = await file.read()
    ext = (file.filename or "upload").rsplit(".", 1)[-1].lower() if file.filename else "jpg"
    local_name = f"{wo['wo_number']}_{uuid.uuid4().hex[:8]}.{ext}"
    local_path = uploads_dir / local_name
    local_path.write_bytes(file_bytes)

    media_type = _CONTENT_TYPE_TO_MEDIA_TYPE.get(content_type, "image")

    # Count existing IT-uploaded media to generate label index
    existing_media = await db.get_media(wo_id)
    it_count = sum(1 for m in existing_media if m["source"] == "issuetracker") + 1
    label = f"IT Photo {it_count}"

    record = await db.add_media(
        wo_id=wo_id,
        label=label,
        source="issuetracker",
        local_filename=local_name,
        media_type=media_type,
    )

    # Push to SC if configured
    sc_action_id = wo.get("sc_action_id")
    if sc_action_id:
        token = os.getenv("SC_API_TOKEN", "")
        if token:
            try:
                sc_media_id = await sc_client.upload_media_to_sc(
                    file_bytes, file.filename or local_name, content_type
                )
                if sc_media_id:
                    attached = await sc_client.attach_media_to_action(sc_action_id, [sc_media_id])
                    if not attached:
                        # SC's timeline/media endpoint is not available; post a comment
                        # as a visible fallback so the upload appears on the action timeline.
                        try:
                            await sc_client.add_action_comment(
                                sc_action_id,
                                f"Photo added from IssueTracker: {label}",
                            )
                        except Exception as ce:
                            logger.warning(
                                "Failed to post fallback comment for media on SC action %s: %s",
                                sc_action_id, ce,
                            )
                    sync_detail = (
                        f"Uploaded media {label} to SC action"
                        if attached
                        else f"Uploaded media {label} to SC storage (comment posted to timeline)"
                    )
                    await db.add_sync_log(
                        direction="IssueTracker → SC",
                        event_type="media",
                        sc_action_id=sc_action_id,
                        wo_number=wo["wo_number"],
                        details=sync_detail,
                    )
                    await sync_engine._broadcast({
                        "type": "sync",
                        "direction": "IssueTracker → SC",
                        "event": "media",
                        "wo_number": wo["wo_number"],
                        "sc_action_id": sc_action_id,
                        "details": f"Media '{label}' uploaded to SafetyCulture",
                    })
            except Exception as e:
                logger.warning("Failed to push media to SC for WO %s: %s", wo["wo_number"], e)

    return record


@app.delete("/api/work-orders/{wo_id}/media/{media_id}")
async def delete_wo_media(wo_id: int, media_id: int):
    """Delete a media record (and local file if applicable)."""
    wo = await db.get_work_order(wo_id)
    if not wo:
        raise HTTPException(404, "Work order not found")

    record = await db.delete_media(media_id)
    if not record:
        raise HTTPException(404, "Media not found")

    # Remove local file if present
    if record.get("local_filename"):
        local = uploads_dir / record["local_filename"]
        if local.exists():
            local.unlink(missing_ok=True)

    return {"status": "ok", "id": media_id}


@app.get("/api/media/{media_id}/proxy")
async def proxy_media(media_id: int):
    """Proxy or redirect to the actual media bytes.

    Routes based on source:
    - issuetracker: redirect to /static/uploads/{filename}
    - sc_action: get SC signed download URL then redirect
    - sc_inspection: proxy raw bytes from SC audit media endpoint
    """
    db_conn = await db.get_db()
    try:
        cursor = await db_conn.execute("SELECT * FROM media WHERE id = ?", (media_id,))
        row = await cursor.fetchone()
    finally:
        await db.release_db(db_conn)

    if not row:
        raise HTTPException(404, "Media not found")

    record = dict(row)
    source = record.get("source", "")

    if source == "issuetracker":
        filename = record.get("local_filename")
        if not filename:
            raise HTTPException(404, "Local file not found")
        return RedirectResponse(f"/static/uploads/{filename}")

    token = os.getenv("SC_API_TOKEN", "")
    if not token:
        raise HTTPException(503, "SC API not configured")

    if source == "sc_action":
        sc_id = record.get("sc_media_id")
        sc_token = record.get("sc_media_token")
        if not sc_id or not sc_token:
            raise HTTPException(404, "Missing SC media credentials")
        signed_url = await sc_client.get_media_download_url(sc_id, sc_token)
        if not signed_url:
            raise HTTPException(502, "Could not obtain SC download URL")
        return RedirectResponse(signed_url)

    if source == "sc_inspection":
        sc_id = record.get("sc_media_id")
        insp_id = record.get("sc_inspection_id")
        if not sc_id or not insp_id:
            raise HTTPException(404, "Missing SC inspection media credentials")
        result = await sc_client.get_inspection_media_bytes(insp_id, sc_id)
        if not result:
            raise HTTPException(502, "Could not fetch SC inspection media")
        content, content_type = result
        return Response(content=content, media_type=content_type)

    raise HTTPException(400, f"Unknown media source: {source}")


@app.get("/api/media")
async def list_all_media():
    """Return all media records with work order info (for the gallery tab)."""
    media = await db.get_all_media()
    return {"media": media}


# ---------------------------------------------------------------------------
# Status / health
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def status():
    token = os.getenv("SC_API_TOKEN", "")
    live = bool(token)
    return {
        "live_sync": live,
        "sync_interval": int(os.getenv("SYNC_INTERVAL_SECONDS", "10")),
    }
