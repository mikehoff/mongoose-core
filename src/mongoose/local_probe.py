"""Loopback probes that understand Mongoose's HTTPS-by-default startup.

Historically internal scripts and status/watchdog code checked http://127.0.0.1:<port>/health.
Mongoose 1.1 turns HTTPS on by default, often with a self-signed certificate. A plain HTTP probe to
that TLS socket fails even though the server is healthy. These helpers try the active local scheme and
fall back the other way, accepting the local self-signed cert only for loopback probes.
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.request
from typing import Any


def _tls_off() -> bool:
    v = (os.getenv("MONGOOSE_TLS") or "").strip().lower()
    return v in {"off", "0", "false", "no", "none"}


def local_base_candidates(port: int | str) -> list[str]:
    port_s = str(port)
    https = f"https://127.0.0.1:{port_s}"
    http = f"http://127.0.0.1:{port_s}"
    return [http, https] if _tls_off() else [https, http]


def preferred_local_base(port: int | str) -> str:
    return local_base_candidates(port)[0]


def _urlopen(url: str, *, timeout: float) -> bytes:
    ctx = None
    if url.startswith("https://"):
        # Local-only readiness/status probe. First-run uses a self-signed certificate by design.
        ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(url, timeout=timeout, context=ctx) as r:  # noqa: S310 - loopback only
        return r.read()


def get_bytes(path: str, *, port: int | str, timeout: float = 2.0) -> tuple[str, bytes]:
    last: Exception | None = None
    for base in local_base_candidates(port):
        url = f"{base}{path}"
        try:
            return url, _urlopen(url, timeout=timeout)
        except Exception as exc:  # pragma: no cover - exact transport errors differ by OS/OpenSSL
            last = exc
    if last:
        raise last
    raise RuntimeError("no local probe candidates")


def get_json(path: str = "/health", *, port: int | str, timeout: float = 2.0) -> tuple[str, dict[str, Any]]:
    url, body = get_bytes(path, port=port, timeout=timeout)
    data = json.loads(body.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{url} did not return a JSON object")
    return url, data


def health(port: int | str, *, timeout: float = 2.0) -> tuple[str, dict[str, Any]]:
    return get_json("/health", port=port, timeout=timeout)


def is_mongoose_health(port: int | str, *, timeout: float = 2.0) -> bool:
    try:
        _url, data = health(port, timeout=timeout)
        return data.get("name") == "mongoose"
    except Exception:
        return False
