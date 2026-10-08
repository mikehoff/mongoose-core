from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

import httpx
from pydantic import BaseModel, Field

from ..db import safe_fts_query, audit, connect, utc_now_iso
from ..settings import get_settings


class NotificationIn(BaseModel):
    source: str
    app: str
    title: str
    body: str = ""
    privacy: str = "medium"
    urgency: str = "normal"
    device: str = "unknown"
    raw: dict[str, Any] = Field(default_factory=dict)


def dedupe_key(n: NotificationIn) -> str:
    material = f"{n.source}|{n.app}|{n.title}|{n.body}|{n.privacy}|{n.urgency}|{n.device}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def route_notification(n: NotificationIn) -> dict[str, Any]:
    privacy = n.privacy.lower()
    urgency = n.urgency.lower()

    speak_allowed = False
    routed_to = "mac_dashboard"

    if privacy in {"high", "secret"}:
        routed_to = "mac_dashboard"
        speak_allowed = False
    elif urgency in {"critical", "high"}:
        routed_to = "pixel_watch"
        speak_allowed = privacy == "low"
    elif privacy == "low":
        routed_to = "room_tablet"
        speak_allowed = True
    else:
        routed_to = "phone"
        speak_allowed = False

    return {"routed_to": routed_to, "speak_allowed": speak_allowed}


async def publish_ntfy(title: str, body: str, priority: str = "default") -> bool:
    from .egress_guard import egress_allowed
    if not egress_allowed("ntfy")[0]:
        return False
    settings = get_settings()
    if not settings.ntfy_url or not settings.ntfy_topic:
        return False
    headers = {"Title": title, "Priority": priority}
    if settings.ntfy_token:
        headers["Authorization"] = f"Bearer {settings.ntfy_token}"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(f"{settings.ntfy_url.rstrip('/')}/{settings.ntfy_topic}", headers=headers, content=body)
            return response.status_code < 400
    except Exception:
        return False


def _whatsapp_notifications_enabled() -> bool:
    import os
    return str(os.getenv("MONGOOSE_WHATSAPP_NOTIFICATIONS") or os.getenv("WHATSAPP_NOTIFICATIONS") or "").lower() in ("1", "true", "yes", "on")


def _whatsapp_owner_fallback(title: str, body: str) -> dict | None:
    """Last-resort owner notification over WhatsApp, from Mongoose's own number to the owner's number.
    Only for the owner/system broadcast path, only when explicitly enabled, and only when Web Push and
    ntfy could not reach the owner. Never used for person-to-person or profile-scoped sends."""
    if not _whatsapp_notifications_enabled():
        return None
    try:
        from .whatsapp import send_mongoose_notification_to_owner
        text = (title or "Mongoose").strip()
        if body:
            text += "\n" + body
        r = send_mongoose_notification_to_owner(text)
        if r.get("ok"):
            return {"sent": True, "channel": "whatsapp", "whatsapp": r}
    except Exception:
        pass
    return None


async def send_to_phone(
    title: str,
    body: str = "",
    *,
    priority: str = "default",
    tags: str | list[str] | None = None,
    click: str | None = None,
    actions: list[dict[str, Any]] | None = None,
    target_user_id: str | None = None,
    target_device_profiles: tuple[str, ...] | list[str] | set[str] | None = None,
    target_device_ids: tuple[str, ...] | list[str] | set[str] | None = None,
    log_source: str = "mongoose",
    log_app: str = "Mongoose",
) -> dict[str, Any]:
    """Send a push notification to the user's phone via ntfy (the local-first push
    channel: the user runs the ntfy app subscribed to their topic). This is the
    first-class 'Mongoose, tell my phone X' capability. Returns whether it went and
    why not if it didn't, so callers and the user can see when ntfy isn't set up.

    DELIVERY MODEL (read before using for person-to-person or owner-scoped features):
    Three targeting modes. (1) System/owner broadcast (neither target set): delivered to all
    account-bound Web Push subscriptions AND to the shared ntfy topic (settings.ntfy_topic). (2) PERSONAL
    (target_user_id set, e.g. poke/song-exchange): delivered ONLY to that user's account-bound Web Push
    subscriptions, NEVER to the shared ntfy broadcast. The shared topic is a broadcast; routing a
    personal poke there would leak it to everyone, so a personal message with no matching subscription
    returns sent=False with reason 'no_target_subscription' rather than falling back to broadcast.
    (3) DEVICE-PROFILE-SCOPED (target_device_profiles set, e.g. owner reminders to primary_phone /
    home_tablet): Web Push is restricted to subscriptions whose stored device_profile is in the set, so
    a trusted-guest PWA does not receive owner reminders. Legacy unscoped subscriptions are still
    included for owner/system profile sends so an existing owner phone keeps working after upgrade; new
    trusted-guest subscriptions are explicitly scoped and excluded.
    (Note: there is still no per-user *ntfy* topic; per-user and per-profile delivery ride on Web Push
    subscriptions bound via /push/subscribe. A recipient who has subscribed no qualifying PWA is
    intentionally left undelivered for personal/scoped sends, not broadcast.)

    priority: ntfy levels max|high|default|low|min.
    tags: ntfy tags (emoji/labels), e.g. 'warning' or ['rotating_light','house'].
    click: a URL the notification opens when tapped.
    actions: ntfy action buttons, e.g.
        [{"action":"http","label":"Send","url":...,"method":"POST","clear":True},
         {"action":"http","label":"Cancel","url":...,"method":"POST"}].
    target_user_id: account id for personal notifications. When supplied, Mongoose
        must NOT fall back to a shared ntfy topic or broadcast Web Push, because that
        would leak one user's poke/song/feedback to everyone.
    target_device_profiles: profile-scoped device notification (for example owner reminders to
        primary_phone/home_tablet). This prevents a trusted_guest PWA receiving owner/system alerts.
    """
    settings = get_settings()

    # Web Push first: this is the no-extra-app channel. If any phone PWA is subscribed, it gets a
    # real notification even when closed. Independent of ntfy, so the user doesn't need the ntfy app.
    web_push_result = {"sent": 0}
    try:
        from .web_push import send_web_push, subscription_count
        scoped_count = subscription_count(
            user_id=target_user_id,
            device_profiles=target_device_profiles,
            device_ids=target_device_ids,
            include_unscoped=True,
        )
        if scoped_count > 0:
            click_url = click or "/device/launchpad"
            tag = (tags if isinstance(tags, str) else (tags[0] if tags else "")) or ""
            web_push_result = await send_web_push(
                title or "Mongoose", body or "", url=click_url, tag=tag,
                user_id=target_user_id, device_profiles=target_device_profiles,
                device_ids=target_device_ids, include_unscoped=True,
            )
    except Exception as exc:
        web_push_result = {"sent": 0, "error": str(exc)}

    # Record every push Mongoose sends in the notifications log, so the phone's "Notifications"
    # list actually reflects what went out (daily brief, pokes, reminders) instead of only inbound
    # or smoke-test rows. Best-effort: a logging hiccup must never block the send.
    try:
        store_notification(NotificationIn(
            source=log_source, app=log_app, title=(title or "Mongoose"), body=(body or ""),
            privacy="private",
            urgency=(priority if priority in ("max", "high", "default", "low", "min") else "normal"),
            device="phone"))
    except Exception:
        pass

    if target_user_id:
        if web_push_result.get("sent", 0) > 0:
            return {"sent": True, "channel": "web_push", "target_user_id": target_user_id,
                    "web_push": web_push_result}
        return {"sent": False, "reason": "no_target_subscription",
                "target_user_id": target_user_id, "web_push": web_push_result,
                "hint": "That person has no account-bound Web Push subscription yet; personal social "
                        "notifications are not sent to the shared ntfy topic."}

    if target_device_profiles:
        if web_push_result.get("sent", 0) > 0:
            return {"sent": True, "channel": "web_push",
                    "target_device_profiles": list(target_device_profiles),
                    "web_push": web_push_result}
        # Do not silently fall back to a shared ntfy topic for profile-scoped owner alerts: after
        # guests exist, a shared topic is not equivalent to "my phone". Surface the missing phone
        # subscription so the UI can tell the owner to enable PWA notifications.
        return {"sent": False, "reason": "no_profile_subscription",
                "target_device_profiles": list(target_device_profiles),
                "web_push": web_push_result,
                "hint": "No owner-profile Web Push subscription is available; open Mongoose on the "
                        "owner phone/tablet and enable notifications."}

    if target_device_ids:
        # SAME guard as target_user_id/target_device_profiles above, for the SAME reason: a
        # device-scoped send (a lounge poke aimed at one person's phone) must never fall
        # through to the shared ntfy topic when that device has no subscription. Found by
        # hardening review after A2: target_device_ids was threaded into the count/send calls
        # but this fallback gate never checked it, so an unsubscribed poke target silently
        # broadcast to ntfy instead of just failing personal delivery.
        if web_push_result.get("sent", 0) > 0:
            return {"sent": True, "channel": "web_push",
                    "target_device_ids": list(target_device_ids), "web_push": web_push_result}
        return {"sent": False, "reason": "no_device_subscription",
                "target_device_ids": list(target_device_ids), "web_push": web_push_result,
                "hint": "That device has no Web Push subscription yet; device-scoped "
                        "notifications are not sent to the shared ntfy topic."}

    if not settings.ntfy_url or not settings.ntfy_topic:
        # No ntfy configured. If web push reached anyone, that's a success on its own.
        if web_push_result.get("sent", 0) > 0:
            return {"sent": True, "channel": "web_push", "web_push": web_push_result}
        return _whatsapp_owner_fallback(title, body) or {"sent": False, "reason": "ntfy_not_configured",
                "web_push": web_push_result,
                "hint": "Install Mongoose on your phone (Add to Home Screen over https) and enable "
                        "notifications, or set NTFY_URL/NTFY_TOPIC and use the ntfy app."}
    # Local-first guard: if the ntfy server isn't local, the title/body will transit
    # a third party. We still send (the user configured it) but flag it so it's not
    # silent, matching the local-first posture.
    is_local = any(h in settings.ntfy_url for h in ("127.0.0.1", "localhost", "192.168.", "10.", "::1"))
    warning = None if is_local else "ntfy server is not local; notification content transited a third party"
    headers = {"Title": title or "Mongoose", "Priority": priority}
    if tags:
        headers["Tags"] = tags if isinstance(tags, str) else ",".join(tags)
    if click:
        headers["Click"] = click
    if actions:
        # ntfy supports a JSON Actions header (or a header-encoded form). JSON is the
        # most expressive: each action becomes a button in the notification.
        import json as _json
        headers["Actions"] = _json.dumps(actions)
    if settings.ntfy_token:
        headers["Authorization"] = f"Bearer {settings.ntfy_token}"
    url = f"{settings.ntfy_url.rstrip('/')}/{settings.ntfy_topic}"
    from .egress_guard import egress_allowed
    if not egress_allowed("ntfy_broadcast")[0]:
        return {"sent": False, "reason": "egress_blocked_non_live"}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(url, headers=headers, content=body or "")
        ok = resp.status_code < 400
        audit("notifications", "send_to_phone", ok, payload={"title": title, "status": resp.status_code})
        if ok:
            res = {"sent": True, "status": resp.status_code, "channel": "ntfy+web_push",
                   "web_push": web_push_result}
            if warning:
                res["warning"] = warning
            return res
        return _whatsapp_owner_fallback(title, body) or {"sent": False, "reason": f"ntfy_http_{resp.status_code}", "web_push": web_push_result}
    except Exception as exc:
        audit("notifications", "send_to_phone", False, payload={"title": title, "error": str(exc)})
        # ntfy failed, but if web push reached someone it's still a partial success
        if web_push_result.get("sent", 0) > 0:
            return {"sent": True, "channel": "web_push", "web_push": web_push_result,
                    "ntfy_error": str(exc)}
        return _whatsapp_owner_fallback(title, body) or {"sent": False, "reason": "ntfy_unreachable", "detail": str(exc),
                "web_push": web_push_result}


def store_notification(n: NotificationIn) -> dict[str, Any]:
    """Synchronously persist a notification AND its FTS row, deduped. Returns
    {inserted, dedupe_key, route, rowid}. This is the sync-safe path used by both the
    async ingest and by other services (e.g. WhatsApp ingestion) so notifications are
    always searchable, never inserted raw without FTS."""
    key = dedupe_key(n)
    route = route_notification(n)
    inserted = False
    rowid = None
    with connect() as con:
        try:
            cur = con.execute(
                """
                INSERT INTO notifications(source, app, title, body, privacy, urgency, device, dedupe_key, routed_to, speak_allowed, received_at, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (n.source, n.app, n.title, n.body, n.privacy, n.urgency, n.device,
                 key, route["routed_to"], 1 if route["speak_allowed"] else 0,
                 utc_now_iso(), json.dumps(n.raw)),
            )
            rowid = cur.lastrowid
            con.execute(
                "INSERT INTO notification_fts(rowid, title, body, source, app) VALUES (?, ?, ?, ?, ?)",
                (rowid, n.title, n.body, n.source, n.app),
            )
            inserted = True
        except sqlite3.IntegrityError:
            inserted = False
    return {"inserted": inserted, "dedupe_key": key, "route": route, "rowid": rowid}


async def ingest_notification(n: NotificationIn) -> dict[str, Any]:
    stored = store_notification(n)
    inserted, key, route = stored["inserted"], stored["dedupe_key"], stored["route"]

    priority = "high" if n.urgency in {"critical", "high"} else "default"
    pushed = False
    if inserted and route["routed_to"] in {"pixel_watch", "phone", "room_tablet"}:
        pushed = await publish_ntfy(n.title, n.body, priority=priority)

    audit("notifications", "ingest", inserted, route=route["routed_to"], privacy=n.privacy, payload={"title": n.title, "pushed": pushed})
    return {"inserted": inserted, "dedupe_key": key, "route": route, "pushed": pushed}


def recent_notifications(hours: int = 24, limit: int = 30) -> list[dict[str, Any]]:
    """Notifications received within the last `hours`, newest first. For the daily
    brief's deterministic signal layer. Includes privacy and urgency so callers can
    decide what is safe to surface and what counts as important."""
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with connect() as con:
        rows = con.execute(
            """
            SELECT id, source, app, title, privacy, urgency, device, received_at
            FROM notifications
            WHERE received_at >= ?
            ORDER BY received_at DESC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def search_notifications(q: str, limit: int = 10) -> list[dict[str, Any]]:
    match = safe_fts_query(q)
    if match is None:
        return []
    with connect() as con:
        rows = con.execute(
            """
            SELECT n.* FROM notification_fts f
            JOIN notifications n ON n.id = f.rowid
            WHERE notification_fts MATCH ?
            ORDER BY bm25(notification_fts), n.received_at DESC
            LIMIT ?
            """,
            (match, limit),
        ).fetchall()
        return [dict(row) for row in rows]
