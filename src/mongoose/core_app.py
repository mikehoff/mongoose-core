"""Standalone mongoose-core server: the RTM, unified search, files, email and voicemail API on its own.

The full assistant (mongoose-assistant) mounts the same router inside its app. Running core alone is for
an RTM-only deployment: the WordPress leaseholder portal plugin talks to these routes, and the Cockpit
RTM screens are not available (they belong to the assistant).

    uvicorn mongoose.core_app:app --port 8765
"""
from __future__ import annotations

from fastapi import FastAPI

from . import __version__
from .rtm_api import router
from .web_common import _COCKPIT_NONCE  # noqa: F401  (tests and the plugin bridge read it)

app = FastAPI(title="Mongoose core", version=__version__)
from .web_common import mount_routes  # noqa: E402
mount_routes(app, router)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "name": "mongoose-core", "version": __version__}


@app.get("/cockpit/nonce")
def cockpit_nonce() -> dict:
    """Same-origin nonce for privileged POSTs, exactly as the assistant serves it."""
    return {"cockpit_nonce": _COCKPIT_NONCE}
