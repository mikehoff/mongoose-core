"""RTM deployment-state record, the single shared truth that keeps four things in sync.

THE PROBLEM THIS SOLVES:
The Mongoose RTM mini-screen, the one-click WordPress deploy, the chat-authored build plan, and the
live example-court.org site are four representations of the same reality. Today they can drift silently:
the screen reads local data, deploy records nothing durable, and the live site's running version is
only known via webhook events that nothing reconciles. The screen can say one thing while the site runs
another, and no one notices. That silent drift is the opposite of delightful.

THE DESIGN: one record, one writer per field, a DERIVED verdict.
A single durable object (app_state key `rtm_deploy_state`) holds the few facts that matter, each
written by exactly one source so they can never contradict each other:

  - approved_plan_*      written by the chat-spec -> plan flow on approval (what the owner signed off)
  - deployed_*           written by the deploy action, atomically on a successful push (what actually
                         went to WordPress: version, time, content hash)
  - live_*               written by the inbound webhook when example-court.org reports its running
                         version (what the site actually runs, confirmed from the live end)

The sync verdict (green/amber/red) is NOT stored. It is computed on every read from those fields, so it
can never be stale or disagree with the facts. Every view (the RTM strip, chat status, the cockpit) is
a pure read of this derived verdict, there is nothing for two screens to get out of step about.

This mirrors the shape already proven elsewhere in the codebase: the resource sentinel and the runtime
watchdog each write one status record that read-only views render. Same discipline, applied to RTM
deploy state.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .. import profile as _profile
from ..db import get_app_state, set_app_state

STATE_KEY = "rtm_deploy_state"

# A live confirmation older than this is treated as stale: the site may have changed under us, or the
# plugin stopped phoning home. Amber, not green. Generous because the plugin only posts on activation
# and on events, not on a heartbeat (yet).
_LIVE_FRESHNESS_SECONDS = 7 * 24 * 3600

# After a deploy, we allow this long for the live site to confirm the new version before we treat the
# silence as a problem. Inside this window the state is amber ("deploying, awaiting confirmation"),
# which is honest: the push happened but the loop has not closed.
_DEPLOY_CONFIRM_GRACE_SECONDS = 15 * 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict[str, Any]:
    raw = get_app_state(STATE_KEY, "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(state: dict[str, Any]) -> None:
    try:
        set_app_state(STATE_KEY, json.dumps(state))
    except Exception:
        pass


# ── one writer per field ──────────────────────────────────────────────────────────────────────────

def record_approved_plan(*, plan_id: str, name: str, summary: str = "",
                         step_count: int | None = None) -> dict[str, Any]:
    """Written by the chat-spec -> plan flow when the owner APPROVES a plan. Records what was signed
    off, so the strip can show 'approved but not yet deployed' as a distinct, honest state."""
    state = _load()
    state["approved_plan"] = {
        "plan_id": plan_id,
        "name": name,
        "summary": summary,
        "step_count": step_count,
        "approved_at": _now_iso(),
    }
    _save(state)
    return state["approved_plan"]


def record_deployed(*, version: str | None, content_hash: str | None = None,
                    plan_id: str | None = None, target: str | None = None) -> dict[str, Any]:
    """Written by the deploy action, atomically on a SUCCESSFUL push. This is the only place
    'deployed_version' is set, so the record can never claim a version that was not actually deployed."""
    state = _load()
    state["deployed"] = {
        "version": version,
        "content_hash": content_hash,
        "plan_id": plan_id,
        "target": target or _profile.site_domain(),
        "deployed_at": _now_iso(),
    }
    _save(state)
    return state["deployed"]


def record_live_confirmation(*, version: str | None, content_hash: str | None = None,
                             source: str | None = None) -> dict[str, Any]:
    """Written by the live end of the loop when the site reports its running build. Two sources reach
    here: the inbound webhook (the plugin posts a deployment_confirmed event carrying PC_RTM_VERSION on
    activation), and the active heartbeat (`verify_live`, which GETs the live build endpoint). Either
    way this is the only authoritative statement of what the site actually runs right now. When a
    content hash is supplied we can compare the exact build, not just the version string."""
    state = _load()
    live = {
        "version": version,
        "source": source or _profile.site_domain(),
        "confirmed_at": _now_iso(),
    }
    if content_hash:
        live["content_hash"] = content_hash
    state["live"] = live
    _save(state)
    return state["live"]


def _record_heartbeat(*, ok: bool, error: str = "") -> None:
    """Remember when the live-verify heartbeat last ran and last succeeded, so the strip can show a
    genuine 'last successful WP heartbeat' rather than inferring liveness from a stale webhook."""
    state = _load()
    hb = state.get("heartbeat") or {}
    now = _now_iso()
    hb["last_attempt_at"] = now
    if ok:
        hb["last_ok_at"] = now
        hb["error"] = ""
    else:
        hb["error"] = (error or "")[:200]
    state["heartbeat"] = hb
    _save(state)


def local_build_info() -> dict[str, Any]:
    """The CURRENT local RTM plugin build: {version, content_hash} for what WOULD be deployed next.

    This is the 'what Mongoose has' end of the strip, distinct from 'what was deployed' and 'what the
    site runs'. The hash is computed over the plugin source tree (stable: sorted relative paths + file
    bytes), matching the artifact wp_plugin builds and deploys. Returns Nones gracefully when there is
    no plugin source on this machine, so the strip can say 'no local build' honestly."""
    import hashlib
    from .wp_plugin import _plugin_dir, plugin_source_status
    info: dict[str, Any] = {"version": None, "content_hash": None}
    try:
        status = plugin_source_status()
        if status.get("ok"):
            info["version"] = status.get("version")
    except Exception:
        pass
    try:
        root = _plugin_dir()
        if root.is_dir():
            h = hashlib.sha256()
            files = sorted(p for p in root.rglob("*") if p.is_file())
            for p in files:
                rel = str(p.relative_to(root)).replace("\\", "/")
                if "/." in ("/" + rel) or rel.startswith("."):
                    continue  # skip dotfiles / VCS noise so the hash tracks real build content
                h.update(rel.encode("utf-8", "ignore"))
                h.update(b"\0")
                try:
                    h.update(p.read_bytes())
                except Exception:
                    pass
                h.update(b"\0")
            info["content_hash"] = h.hexdigest()[:16]
    except Exception:
        pass
    return info


def verify_live(*, timeout: float = 6.0) -> dict[str, Any]:
    """Actively close the publish/verify loop: GET the live build endpoint on the WordPress site and
    record what it reports. This is the heartbeat half of 'is example-court.org running this exact build'.

    The live RTM plugin registers under the example-court/v1 namespace. We prefer a /build endpoint
    (version + content_hash, so we can verify the exact build) and fall back to /health (version only),
    which every shipped plugin exposes. The old /wp-json/mongoose/v1/build path never existed on the
    plugin and always 404'd, which is what made the card cry 'nothing deployed'.
    On success we record a live confirmation (version + hash when present) and a successful heartbeat; on
    any failure (no creds, network down, unexpected shape) we record a heartbeat error and return
    ok:false, so the strip degrades honestly instead of pretending. Never raises."""
    from .wp_plugin import _creds
    creds = _creds()
    if not creds:
        _record_heartbeat(ok=False, error="WordPress not configured (WP_URL/WP_USERNAME/WP_APP_PASSWORD)")
        return {"ok": False, "reason": "wp_not_configured"}
    base = creds[0]
    candidates = [
        base.rstrip("/") + _profile.wp_route("/build"),
        base.rstrip("/") + _profile.wp_route("/health"),
    ]
    data = None
    last_err = ""
    try:
        import httpx
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            for url in candidates:
                try:
                    r = client.get(url)
                except Exception as exc:
                    last_err = f"{type(exc).__name__}: {exc}"
                    continue
                if r.status_code == 200:
                    try:
                        parsed = r.json()
                    except Exception:
                        parsed = None
                    if isinstance(parsed, dict):
                        data = parsed
                        break
                    last_err = f"unexpected body from {url}"
                else:
                    last_err = f"HTTP {r.status_code} from {url}"
    except Exception as exc:
        _record_heartbeat(ok=False, error=f"{type(exc).__name__}: {exc}")
        return {"ok": False, "reason": "unreachable", "error": str(exc)}
    if not isinstance(data, dict):
        _record_heartbeat(ok=False, error=last_err or f"no usable response from {_profile.wp_namespace()}")
        return {"ok": False, "reason": "no_data", "error": last_err}
    version = data.get("version")
    content_hash = data.get("content_hash")
    record_live_confirmation(version=str(version) if version is not None else None,
                             content_hash=str(content_hash) if content_hash else None,
                             source=base)
    _record_heartbeat(ok=True)
    return {"ok": True, "version": version, "content_hash": content_hash}


# ── the derived verdict (never stored) ──────────────────────────────────────────────────────────────

def _age_seconds(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return None


def sync_status() -> dict[str, Any]:
    """Compute the live sync verdict between Mongoose and example-court.org from the recorded facts.

    Verdict:
      - green   deployed_version == live_version and the live confirmation is fresh: the screen, the
                deploy and the live site all agree.
      - amber   a deploy happened but the live site has not confirmed yet (inside the grace window), or
                the confirmation is stale, or there is an approved plan not yet deployed: a known,
                expected in-between, shown honestly rather than as either a lie or an alarm.
      - red     deployed_version and live_version disagree (true drift), or we have a deploy but no live
                confirmation past the grace window: the two have diverged and it needs attention.
      - unknown nothing has been deployed yet, or no data: we say so rather than fake green.

    Returns a small JSON-safe dict the strip and chat render directly.
    """
    state = _load()
    approved = state.get("approved_plan") or {}
    deployed = state.get("deployed") or {}
    live = state.get("live") or {}

    dep_ver = deployed.get("version")
    live_ver = live.get("version")
    live_age = _age_seconds(live.get("confirmed_at"))
    dep_age = _age_seconds(deployed.get("deployed_at"))

    site = _profile.site_domain()
    level = "unknown"
    headline = "No deployment recorded yet"
    detail = ""

    if not deployed:
        if live_ver and live_age is not None and live_age <= _LIVE_FRESHNESS_SECONDS:
            # The plugin is usually installed by hand (a zip), so Mongoose holds no 'deployed' record of
            # its own. When the live site itself confirms a running version, that IS the truth for what
            # example-court.org runs, trust it instead of pretending nothing is there.
            level = "green"
            headline = f"{site} is running v{live_ver}"
            detail = "Confirmed live from the site (installed outside Mongoose's one-click deploy)."
        elif live_ver:
            level = "amber"
            headline = f"{site} last reported v{live_ver}"
            detail = "The live confirmation is stale, tap Verify live site to refresh it."
        elif approved:
            level = "amber"
            headline = "Plan approved, not yet deployed"
            detail = f"'{approved.get('name','plan')}' is approved and waiting for one-click deploy."
        else:
            level = "unknown"
            headline = f"Nothing deployed to {site} yet"
    elif dep_ver and live_ver and dep_ver == live_ver and live_age is not None and live_age <= _LIVE_FRESHNESS_SECONDS:
        level = "green"
        headline = f"In sync with {site} (v{dep_ver})"
        detail = f"Live site confirmed running v{live_ver}."
    elif dep_ver and live_ver and dep_ver != live_ver:
        level = "red"
        headline = f"Out of sync with {site}"
        detail = f"Mongoose deployed v{dep_ver}; live site reports v{live_ver}."
    elif dep_ver and not live_ver:
        if dep_age is not None and dep_age <= _DEPLOY_CONFIRM_GRACE_SECONDS:
            level = "amber"
            headline = f"Deploying v{dep_ver}, awaiting confirmation"
            detail = "Pushed to WordPress; waiting for the live site to confirm it is running."
        else:
            level = "red"
            headline = f"Deployed v{dep_ver}, no confirmation from the live site"
            detail = f"The push happened but {site} has not reported the new version. Check the plugin webhook."
    elif dep_ver and live_ver and dep_ver == live_ver and (live_age is None or live_age > _LIVE_FRESHNESS_SECONDS):
        level = "amber"
        headline = f"Versions match (v{dep_ver}), confirmation is stale"
        detail = "Last confirmation from the live site is old; it may have changed since."
    else:
        level = "amber"
        headline = "Deployment state incomplete"

    # A pending approved plan that is newer than what is deployed is worth surfacing even when green.
    pending_plan = None
    if approved and (not deployed or approved.get("plan_id") != deployed.get("plan_id")):
        pending_plan = {"name": approved.get("name"), "approved_at": approved.get("approved_at")}

    # Build-hash awareness (stronger than the version string: catches a rebuild that kept the same
    # version number). Only compared when both ends actually reported a hash, so version-only history
    # keeps its existing verdict.
    dep_hash = deployed.get("content_hash")
    live_hash = live.get("content_hash")
    build_mismatch = bool(dep_hash and live_hash and dep_hash != live_hash)
    if level == "green" and build_mismatch:
        level = "amber"
        headline = f"Same version (v{dep_ver}) but a different build is live"
        detail = f"The version numbers match, but the live build hash differs, {site} is running a different build than Mongoose deployed."

    local = local_build_info()
    local_hash = local.get("content_hash")
    # A newer local build than what was deployed is the cue for one-tap 'deploy latest'.
    local_ahead = bool(local_hash and dep_hash and local_hash != dep_hash)

    heartbeat = state.get("heartbeat") or {}

    friendly = {
        "green": (f"{site} is running the RTM build Mongoose deployed (v{dep_ver})."
                  if dep_ver else f"{site} is running v{live_ver}."),
        "red": f"{site} is out of sync with Mongoose, the live site is not running the deployed build.",
        "unknown": f"Nothing has been deployed to {site} yet.",
    }.get(level, headline)
    if level == "amber":
        friendly = headline if headline else f"{site} deployment is mid-flight."
    if local_ahead and level in ("green", "amber"):
        friendly += " A newer local build is ready to deploy."

    return {
        "level": level,
        "headline": headline,
        "detail": detail,
        "friendly_line": friendly,
        "local_version": local.get("version"),
        "local_hash": local_hash,
        "deployed_version": dep_ver,
        "deployed_hash": dep_hash,
        "deployed_at": deployed.get("deployed_at"),
        "live_version": live_ver,
        "live_hash": live_hash,
        "live_confirmed_at": live.get("confirmed_at"),
        "live_confirmation_age_seconds": round(live_age, 1) if live_age is not None else None,
        "build_mismatch": build_mismatch,
        "local_ahead": local_ahead,
        "heartbeat_last_ok_at": heartbeat.get("last_ok_at"),
        "heartbeat_last_attempt_at": heartbeat.get("last_attempt_at"),
        "heartbeat_error": heartbeat.get("error") or "",
        "pending_plan": pending_plan,
        "target": deployed.get("target") or site,
    }
