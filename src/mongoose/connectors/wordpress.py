"""WordPress REST API connector for Example Court RTM site and any other WP site.

Uses the WP Application Password flow (Settings → Users → Application Passwords in WP admin).
Configure with WP_URL, WP_USERNAME, WP_APP_PASSWORD in .env. Never stores or sends the password
to any third party: used only for HTTP Basic auth to the configured site's own REST API.

Coverage:
  - Post a notice/announcement (create draft or publish)
  - List recent posts
  - Get a single post
  - Update an existing post

Designed for: posting Example Court service-charge summaries, meeting minutes, beta release notes,
and RTM announcements from within Mongoose or via the chat tool.
"""
from __future__ import annotations

from typing import Any

import httpx

from ..settings import get_settings


def _wp_creds() -> tuple[str, str, str] | None:
    """Return (base_url, username, app_password) or None if not configured."""
    s = get_settings()
    url = (getattr(s, "wp_url", "") or "").rstrip("/")
    user = getattr(s, "wp_username", "") or ""
    pwd = getattr(s, "wp_app_password", "") or ""
    if not (url and user and pwd):
        return None
    return url, user, pwd


def _api(path: str) -> str:
    creds = _wp_creds()
    if not creds:
        return ""
    return f"{creds[0]}/wp-json/wp/v2/{path.lstrip('/')}"


def wp_status() -> dict[str, Any]:
    """Check WordPress connectivity and authentication. Read-only."""
    creds = _wp_creds()
    if not creds:
        return {"ok": False, "configured": False,
                "reason": "WP_URL, WP_USERNAME, and WP_APP_PASSWORD not set"}
    url, user, pwd = creds
    try:
        r = httpx.get(f"{url}/wp-json/wp/v2/users/me",
                      auth=(user, pwd), timeout=8)
        if r.status_code == 200:
            d = r.json()
            return {"ok": True, "configured": True, "user": d.get("name"), "url": url,
                    "roles": d.get("roles", [])}
        if r.status_code == 401:
            return {"ok": False, "configured": True,
                    "reason": "Authentication failed: check WP_USERNAME and WP_APP_PASSWORD"}
        return {"ok": False, "configured": True, "reason": f"HTTP {r.status_code}"}
    except Exception as exc:
        return {"ok": False, "configured": True, "reason": f"unreachable: {exc}"}


def create_post(title: str, content: str, *,
                status: str = "draft",
                categories: list[int] | None = None,
                tags: list[int] | None = None,
                excerpt: str = "") -> dict[str, Any]:
    """Create a WordPress post. status: 'draft' (default, safe) or 'publish'.
    Returns {ok, id, url, status, edit_url} on success."""
    from ..services.egress_guard import egress_allowed
    if not egress_allowed("wp_cms_write")[0]:
        return {"ok": False, "reason": "egress_blocked_non_live"}
    creds = _wp_creds()
    if not creds:
        return {"ok": False, "reason": "not_configured"}
    _, user, pwd = creds
    payload: dict[str, Any] = {"title": title, "content": content, "status": status}
    if excerpt:
        payload["excerpt"] = excerpt
    if categories:
        payload["categories"] = categories
    if tags:
        payload["tags"] = tags
    try:
        r = httpx.post(_api("posts"), auth=(user, pwd), json=payload, timeout=12)
        if r.status_code in (200, 201):
            d = r.json()
            return {"ok": True, "id": d.get("id"), "url": d.get("link"),
                    "status": d.get("status"), "edit_url": d.get("link", "").replace("/?p=", "/wp-admin/post.php?post=") + "&action=edit"}
        return {"ok": False, "reason": f"HTTP {r.status_code}", "detail": r.text[:300]}
    except Exception as exc:
        return {"ok": False, "reason": f"request_failed: {exc}"}


def update_post(post_id: int, *,
                title: str | None = None,
                content: str | None = None,
                status: str | None = None) -> dict[str, Any]:
    """Update a WordPress post (title, content, or status)."""
    from ..services.egress_guard import egress_allowed
    if not egress_allowed("wp_cms_write")[0]:
        return {"ok": False, "reason": "egress_blocked_non_live"}
    creds = _wp_creds()
    if not creds:
        return {"ok": False, "reason": "not_configured"}
    _, user, pwd = creds
    payload: dict[str, Any] = {}
    if title is not None:
        payload["title"] = title
    if content is not None:
        payload["content"] = content
    if status is not None:
        payload["status"] = status
    if not payload:
        return {"ok": False, "reason": "nothing to update"}
    try:
        r = httpx.post(_api(f"posts/{post_id}"), auth=(user, pwd), json=payload, timeout=12)
        if r.status_code in (200, 201):
            d = r.json()
            return {"ok": True, "id": d.get("id"), "status": d.get("status"), "url": d.get("link")}
        return {"ok": False, "reason": f"HTTP {r.status_code}", "detail": r.text[:300]}
    except Exception as exc:
        return {"ok": False, "reason": f"request_failed: {exc}"}


def list_posts(limit: int = 10, status: str = "any") -> dict[str, Any]:
    """List recent WordPress posts."""
    creds = _wp_creds()
    if not creds:
        return {"ok": False, "reason": "not_configured", "posts": []}
    _, user, pwd = creds
    params: dict[str, Any] = {"per_page": min(limit, 50), "orderby": "date", "order": "desc"}
    if status != "any":
        params["status"] = status
    try:
        r = httpx.get(_api("posts"), auth=(user, pwd), params=params, timeout=8)
        if r.status_code == 200:
            posts = [{"id": p["id"], "title": p["title"]["rendered"],
                      "status": p["status"], "url": p["link"],
                      "date": p["date"]} for p in r.json()]
            return {"ok": True, "posts": posts, "count": len(posts)}
        return {"ok": False, "reason": f"HTTP {r.status_code}", "posts": []}
    except Exception as exc:
        return {"ok": False, "reason": f"request_failed: {exc}", "posts": []}
