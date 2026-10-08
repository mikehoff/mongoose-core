"""RTM build source-of-truth status.

This module answers the repeated real-user/auditor question: when RTM product work can be kicked off
from chat, UI, Claude/Code, or protocol-v3 peers, which state is canonical and what is merely a
proposal? It is read-only and intentionally lightweight: it inspects local docs/build/update/plugin
state without contacting WordPress or the Pi.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]



def _recent_plugin_builds(limit: int = 5) -> list[dict[str, Any]]:
    """List recent plugin build zips without creating the build directory.

    The WordPress plugin service's public list_builds() helper creates data/builds/plugin as
    a side effect. Source-of-truth status is a read-only/auditor surface, so it must inspect
    the expected directory directly and return an empty list when no builds exist.
    """
    data_dir = (os.getenv("MONGOOSE_DATA_DIR") or "").strip() or os.path.expanduser("~/.mongoose/data")
    build_dir = Path(data_dir).expanduser() / "builds" / "plugin"
    if not build_dir.exists() or not build_dir.is_dir():
        return []
    builds: list[dict[str, Any]] = []
    for z in sorted(build_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
        st = z.stat()
        builds.append({
            "name": z.name,
            "path": str(z),
            "size_bytes": st.st_size,
            "modified": st.st_mtime,
        })
        if len(builds) >= limit:
            break
    return builds


def _doc_status(root: Path, rel: str) -> dict[str, Any]:
    p = root / rel
    return {"path": rel, "present": p.exists(), "size_bytes": p.stat().st_size if p.exists() else None}


def source_of_truth_status() -> dict[str, Any]:
    """Return a concise, machine-readable RTM build provenance map.

    The status deliberately separates Example Court RTM *operations* (demands, statements, service
    charge data) from the RTM *product build* (plugin/frontend/backend/update candidates). Claude and
    v3 peers can suggest or coordinate work, but until a candidate is staged/checked/applied, persisted
    in the local build/plugin/update state, it is not canonical.
    """
    root = _repo_root()
    docs = [
        _doc_status(root, "docs/RTM_BUILD_SOURCE_OF_TRUTH.md"),
        _doc_status(root, "docs/BUILD_RUNNER.md"),
        _doc_status(root, "docs/RTM_MANAGEMENT.md"),
        _doc_status(root, "docs/PROTOCOL_V3_COORDINATION.md"),
        _doc_status(root, "docs/REQUIREMENTS_TRACEABILITY.md"),
        _doc_status(root, "docs/AUDITOR_EXPERIENCE_CHECKLIST.md"),
    ]
    try:
        from ..updater import current_version, list_update_stages
        update_state = {"version": current_version(), "stages": list_update_stages(limit=5)}
    except Exception as exc:  # noqa: BLE001
        update_state = {"error": f"update state unavailable: {type(exc).__name__}: {exc}"[:180]}
    try:
        from .wp_plugin import plugin_source_status
        plugin_state = {"source": plugin_source_status(), "recent_builds": _recent_plugin_builds()}
    except Exception as exc:  # noqa: BLE001
        plugin_state = {"error": f"plugin state unavailable: {type(exc).__name__}: {exc}"[:180]}
    try:
        from ..settings import get_settings
        s = get_settings()
        config_refs = {
            "data_dir": str(getattr(s, "data_dir", "")),
            "wp_configured": bool(getattr(s, "wp_url", "") or getattr(s, "wordpress_url", "")),
            "v3_enabled": bool(getattr(s, "v3_enabled", False)),
        }
    except Exception as exc:  # noqa: BLE001
        config_refs = {"error": f"settings unavailable: {type(exc).__name__}: {exc}"[:180]}
    return {
        "ok": True,
        "canonical": {
            "rtm_operations": "RTM management/service-charge state lives in the RTM data store and docs/RTM_MANAGEMENT.md.",
            "rtm_product_build": "Build candidates, staged updates, plugin source/build zips and applied runtime state are canonical; chat/UI/v3 proposals are not canonical until staged/checked/applied or written into the documented source tree.",
            "main_doc": "docs/RTM_BUILD_SOURCE_OF_TRUTH.md",
        },
        "entry_points": {
            "ui": ["/build/status", "/build/stage", "/build/deploy", "/wp-plugin/*", "/rtm/manage/*"],
            "chat": ["rtm_build_source", "wp_plugin_status", "wp_plugin_action after sensitive confirmation", "check_update/apply_update after sensitive confirmation"],
            "protocol_v3_peers": "Can coordinate/announce/reconcile work; they do not outrank local persisted build/update/plugin state.",
            "claude_or_code_thread": "May produce patches/spec updates; canonical only after the patch is staged, checked, applied, and relevant docs/state are updated.",
        },
        "docs": docs,
        "update_state": update_state,
        "plugin_state": plugin_state,
        "config_refs": config_refs,
        "guardrails": [
            "A chat request to build/deploy/promote must become a sensitive proposal first, then require explicit confirmation.",
            "UI builds and chat builds converge through the same staged update/plugin build state, not separate private paths.",
            "Operational RTM records are not the same as the RTM product build backlog/spec.",
        ],
    }
