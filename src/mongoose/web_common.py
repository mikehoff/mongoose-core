"""Request guards and small HTTP helpers shared by every Mongoose router.

Lives in mongoose-core so the RTM / search API and the assistant app enforce the same
credentials: the per-process Cockpit nonce (browser CSRF layer) and the API token (phone)."""
from __future__ import annotations

import secrets as _secrets

from fastapi import HTTPException, Request
import threading as _status_threading
import time as _status_time

# A per-process Cockpit action nonce. The cockpit page reads it from /cockpit/nonce (same origin)
# and must echo it on privileged POSTs. A cross-origin page that cannot read it cannot forge them.
_COCKPIT_NONCE = _secrets.token_urlsafe(24)


def _require_cockpit_nonce(request) -> None:
    """Raise 403 unless the request carries the cockpit nonce (header or body)."""
    supplied = ""
    try:
        supplied = request.headers.get("x-cockpit-nonce", "") if hasattr(request, "headers") else ""
    except Exception:
        supplied = ""
    if not supplied or not _secrets.compare_digest(supplied, _COCKPIT_NONCE):
        raise HTTPException(status_code=403, detail="invalid or missing cockpit_nonce")


def _require_token_or_cockpit_nonce(request) -> None:
    """For actions reachable from BOTH the phone (ntfy, sends X-Mongoose-Token) and
    the Cockpit (browser, sends X-Cockpit-Nonce): accept either credential. The
    cockpit nonce is the CSRF layer for the browser; the API token covers the phone.
    A request with neither is rejected, so injected same-origin script (which can't
    read either secret cross-origin) can't forge the action."""
    from .settings import get_settings
    try:
        headers = request.headers
    except Exception:
        raise HTTPException(status_code=403, detail="missing credentials")
    nonce = headers.get("x-cockpit-nonce", "")
    if nonce and _secrets.compare_digest(nonce, _COCKPIT_NONCE):
        return
    token = getattr(get_settings(), "api_token", "") or ""
    supplied = headers.get("x-mongoose-token", "")
    if token and supplied and _secrets.compare_digest(supplied, token):
        return
    raise HTTPException(status_code=403,
                        detail="action requires X-Cockpit-Nonce (cockpit) or X-Mongoose-Token (phone)")


_STATUS_CACHE: dict = {}
_STATUS_REFRESHING: set = set()
_STATUS_LOCK = _status_threading.Lock()


def _status_cached(key: str, ttl: float, fn):
    """Stale-while-revalidate cache for a GLOBAL read-only status report. Once the value is older than
    ttl we serve the STALE value immediately and recompute in a background thread (deduped per key), so
    no interactive request ever pays the cold cost (readiness is ~9s on the busy live server). Only the
    very first call after boot computes synchronously."""
    now = _status_time.time()
    hit = _STATUS_CACHE.get(key)
    if hit is None:
        val = fn()
        _STATUS_CACHE[key] = (_status_time.time(), val)
        return val
    ts, val = hit
    if now - ts >= ttl:
        with _STATUS_LOCK:
            if key not in _STATUS_REFRESHING:
                _STATUS_REFRESHING.add(key)

                def _refresh(k=key, f=fn):
                    try:
                        v = f()
                        _STATUS_CACHE[k] = (_status_time.time(), v)
                    except Exception:
                        pass
                    finally:
                        _STATUS_REFRESHING.discard(k)
                _status_threading.Thread(target=_refresh, daemon=True).start()
    return val


async def _read_upload(request: Request):
    """Pull (filename, bytes, mime) from either a multipart 'file' field or a raw body with X-Filename.
    Returns (filename, None, mime) when there is nothing to read."""
    ctype = request.headers.get("content-type", "") or ""
    if "multipart/form-data" in ctype:
        try:
            form = await request.form()
        except Exception:
            return ("", None, None)
        up = form.get("file")
        if up is None:
            return ("", None, None)
        data = await up.read()
        return (getattr(up, "filename", "") or "upload.bin", data, getattr(up, "content_type", None))
    filename = request.headers.get("x-filename", "") or request.query_params.get("filename", "") or "upload.bin"
    data = await request.body()
    return (filename, (data or None), request.headers.get("content-type") or None)


def _safe_download_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_. " else "_" for c in (name or "download"))[:120].strip() or "download"


def mount_routes(app, router) -> None:
    """Register a router's routes on the app as ordinary, flat routes. FastAPI's include_router keeps the
    router as one nested entry in app.routes, which hides its paths from code that lists app.routes
    (the capability index, the system map, route inventories). Flat routes keep those surfaces complete
    and behave exactly like routes declared on the app itself."""
    for route in router.routes:
        app.router.routes.append(route)
