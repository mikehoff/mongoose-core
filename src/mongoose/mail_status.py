from __future__ import annotations

import asyncio
import json
from typing import Any

# Mail sync status + on-demand sync.
#
# The sync ENGINES already exist (IMAP cursor sync, Gmail and Microsoft connectors,
# all driven by the scheduler's sync_email job). What was missing is VISIBILITY: a
# user configuring real mail had no way to see "is this provider authorised, and is my
# mail actually flowing?" beyond reading scheduler logs. This composes the existing
# pieces (oauth_status + the crawl_sources freshness table + a message count) into a
# clear per-provider picture, and offers a one-shot "sync now" that reports what
# happened. It adds no new sync capability; it makes the existing one legible.


def _message_count() -> int:
    try:
        from .db import connect
        with connect() as con:
            return con.execute("SELECT COUNT(*) FROM email_messages").fetchone()[0]
    except Exception:
        return 0


def _source_freshness() -> dict[str, dict[str, Any]]:
    """Per-source last-completed / status / indexed from the crawler's source table,
    keyed by a coarse provider label (imap/gmail/microsoft)."""
    out: dict[str, dict[str, Any]] = {}
    try:
        from .db import connect
        with connect() as con:
            rows = con.execute(
                "SELECT source, kind, last_completed, last_status, last_error, indexed "
                "FROM crawl_sources").fetchall()
        for r in rows:
            d = dict(r)
            kind = (d.get("kind") or "").lower()
            src = (d.get("source") or "").lower()
            if kind == "imap" or src.startswith("imap"):
                label = "imap"
            elif "gmail" in src or "google" in src:
                label = "gmail"
            elif "microsoft" in src or "graph" in src or "outlook" in src:
                label = "microsoft"
            else:
                continue
            # keep the most recently completed row per provider
            prev = out.get(label)
            if not prev or (d.get("last_completed") or "") > (prev.get("last_completed") or ""):
                out[label] = d
    except Exception:
        pass
    return out


def mail_status() -> dict[str, Any]:
    """Per-provider mail-sync status: configured, authorised, last sync, freshness.
    Never raises."""
    from pathlib import Path
    from .settings import get_settings
    settings = get_settings()
    providers: list[dict[str, Any]] = []
    fresh = _source_freshness()

    # IMAP: configured by host+credentials (no OAuth).
    imap_cfg = bool(getattr(settings, "imap_host", "") and getattr(settings, "imap_username", ""))
    imap_fresh = fresh.get("imap", {})
    providers.append({
        "provider": "imap",
        "configured": imap_cfg,
        "authorized": imap_cfg,  # IMAP "auth" is just having credentials
        "last_completed": imap_fresh.get("last_completed"),
        "last_status": imap_fresh.get("last_status"),
        "indexed": imap_fresh.get("indexed"),
        "note": "" if imap_cfg else "set MONGOOSE_IMAP_HOST / USERNAME / PASSWORD",
    })

    # OAuth providers: authorised iff a token is present.
    try:
        from .oauth import oauth_status
        oauth = oauth_status()
    except Exception:
        oauth = {}
    g_cfg = bool(getattr(settings, "google_client_secrets_path", "")
                 and Path(settings.google_client_secrets_path).exists())
    g_auth = bool(oauth.get("google"))
    g_fresh = fresh.get("gmail", {})
    providers.append({
        "provider": "gmail",
        "configured": g_cfg,
        "authorized": g_auth,
        "last_completed": g_fresh.get("last_completed"),
        "last_status": g_fresh.get("last_status"),
        "indexed": g_fresh.get("indexed"),
        "note": ("" if g_auth else ("run `mongoose authorize google`" if g_cfg
                 else "add Google client-secrets JSON, then `mongoose authorize google`")),
    })

    m_cfg = bool(getattr(settings, "ms_client_id", ""))
    m_auth = bool(oauth.get("microsoft"))
    m_fresh = fresh.get("microsoft", {})
    providers.append({
        "provider": "microsoft",
        "configured": m_cfg,
        "authorized": m_auth,
        "last_completed": m_fresh.get("last_completed"),
        "last_status": m_fresh.get("last_status"),
        "indexed": m_fresh.get("indexed"),
        "note": ("" if m_auth else ("run `mongoose authorize microsoft`" if m_cfg
                 else "set MONGOOSE_MS_CLIENT_ID, then `mongoose authorize microsoft`")),
    })

    any_active = any(p["authorized"] for p in providers)
    progress = None
    try:
        from .db import get_app_state
        raw = get_app_state("mail_sync_progress", "") or ""
        progress = json.loads(raw) if raw else None
    except Exception:
        progress = None
    return {
        "providers": providers,
        "message_count": _message_count(),
        "any_active": any_active,
        "sync_progress": progress,
    }


def _set_progress(payload: dict[str, Any]) -> None:
    """Record mail-sync progress so the cockpit can show 'connecting / syncing / complete' instead of a
    spinner that never resolves."""
    try:
        from .db import set_app_state, utc_now_iso
        payload.setdefault("updated_at", utc_now_iso())
        set_app_state("mail_sync_progress", json.dumps(payload, sort_keys=True))
    except Exception:
        pass


def _run_scheduler_mail_job_sync() -> dict[str, Any]:
    """Run the scheduler's async mail job in a worker thread. The job calls SYNCHRONOUS IMAP/Gmail/
    Microsoft clients; running it directly on the FastAPI event loop froze the whole cockpit while
    mail/network IO was in flight (a prime suspect for the long unresponsive gaps)."""
    try:
        from .services.scheduler import _job_sync_email
    except ImportError:  # mongoose-core on its own: mail sync is driven by the assistant's scheduler
        return {"ok": False, "error": "mail sync runs from the assistant's scheduler (mongoose-assistant)"}
    return asyncio.run(_job_sync_email())


async def sync_now() -> dict[str, Any]:
    """Run one mail sync cycle across configured providers and report what happened. Reuses the
    scheduler's sync_email job (identical to the unattended path), but OFF the event loop and with
    progress, so the cockpit stays responsive and the user sees movement."""
    before = _message_count()
    _set_progress({"running": True, "stage": "starting", "message": "Starting mail sync",
                   "message_count_before": before})
    try:
        _set_progress({"running": True, "stage": "syncing",
                       "message": "Connecting to mail providers and fetching new messages",
                       "message_count_before": before})
        result = await asyncio.to_thread(_run_scheduler_mail_job_sync)
    except Exception as exc:
        _set_progress({"running": False, "stage": "failed", "message": str(exc),
                       "message_count_before": before})
        return {"ok": False, "error": str(exc), "new_messages": 0}
    after = _message_count()
    response = {"ok": True, "providers": result, "new_messages": max(0, after - before),
                "message_count": after}
    _set_progress({"running": False, "stage": "complete", "message": "Mail sync complete",
                   "message_count_before": before, "message_count_after": after,
                   "new_messages": response["new_messages"]})
    return response
