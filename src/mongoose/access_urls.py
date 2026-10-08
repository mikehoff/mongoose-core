"""User-facing URLs for the Mongoose web UI.

Internal liveness probes may safely use loopback, but anything shown to a human or embedded in a
pairing/open URL must use an address that makes sense in a browser. In particular:

* 127.0.0.1 is the safest owner-local browser target: unlike "localhost", it can't be shadowed by
  a browser trying IPv6 first and finding nothing listening, and it avoids the separate-origin
  certificate-trust/service-worker state that "localhost" and "127.0.0.1" track independently.
* 0.0.0.0 is a bind address, not a browser destination.
* HTTPS is the default unless explicitly disabled with MONGOOSE_TLS=off.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse


def tls_off() -> bool:
    v = (os.getenv("MONGOOSE_TLS") or "").strip().lower()
    return v in {"off", "0", "false", "no", "none"}


def default_scheme() -> str:
    """Scheme the default server speaks.

    `mongoose serve` auto-provisions HTTPS unless MONGOOSE_TLS is explicitly off, so absence of
    MONGOOSE_TLS_CERTFILE/KEYFILE does not mean HTTP any more.
    """
    return "http" if tls_off() else "https"


def primary_lan_ip() -> str:
    try:
        from .discovery import _primary_ip
        ip = _primary_ip()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    # fallback: choose the source IP the OS would use for an outbound route (no UDP payload is sent)
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        finally:
            s.close()
    except Exception:
        pass
    return "localhost"


def _normalise_base(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return url
    return ""


def configured_public_base() -> str:
    """Configured public/DDNS URL if the owner has provided one, otherwise empty."""
    # Explicit public URL wins over the DDNS assembler (a deliberate manual override should
    # never be silently replaced), but a stale http:// override must still be re-checked
    # against whether HTTPS is actually available now, or it advertises the wrong scheme
    # forever once the owner later enables it.
    explicit = _normalise_base(os.getenv("MONGOOSE_PUBLIC_URL", ""))
    if explicit:
        try:
            from .ddns import upgrade_stale_public_scheme
            explicit = upgrade_stale_public_scheme(explicit)
        except Exception:
            pass
        return explicit
    # If DDNS is configured, reuse the existing assembler. It can legitimately return None.
    try:
        from .ddns import public_url
        return _normalise_base(public_url() or "")
    except Exception:
        return ""


def local_browser_base(port: int | str | None = None, *, scheme: str | None = None) -> str:
    from .settings import get_settings
    p = str(port or getattr(get_settings(), "port", 8765))
    return f"{scheme or default_scheme()}://127.0.0.1:{p}"


def lan_browser_base(port: int | str | None = None, *, scheme: str | None = None) -> str:
    from .settings import get_settings
    p = str(port or getattr(get_settings(), "port", 8765))
    return f"{scheme or default_scheme()}://{primary_lan_ip()}:{p}"


def is_lan_enabled(host: str | None = None) -> bool:
    try:
        from .settings import get_settings
        s = get_settings()
        h = host if host is not None else getattr(s, "host", "127.0.0.1")
        allow = bool(getattr(s, "allow_remote", False))
    except Exception:
        h = host or os.getenv("MONGOOSE_HOST", "127.0.0.1")
        allow = (os.getenv("MONGOOSE_ALLOW_REMOTE", "").strip().lower() in {"1", "true", "yes", "on"})
    return h in {"0.0.0.0", "::"} or allow


def user_facing_base(port: int | str | None = None, *, scheme: str | None = None,
                     prefer_public: bool = False, prefer_lan: bool = True,
                     host: str | None = None) -> str:
    """Best URL to show/open for a human.

    Prefer a configured public URL only when requested. Otherwise, use the LAN URL when the server is
    meant to be reachable beyond the Mac, and fall back to localhost for Mac-only installs. Never emits
    0.0.0.0 or 127.0.0.1.
    """
    if prefer_public:
        public = configured_public_base()
        if public:
            return public
    if prefer_lan and is_lan_enabled(host):
        return lan_browser_base(port, scheme=scheme)
    return local_browser_base(port, scheme=scheme)


def with_path(base: str, path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return base.rstrip("/") + path
