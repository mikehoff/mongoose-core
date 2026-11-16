"""Site profile: every value that makes this install *this* household and *this* building.

The code is the same for everyone. What differs between a public checkout and a real install is data:
the owner's name, the site domain, the RTM company's slug and block scheme, the WordPress REST
namespace, the voicemail provider's sender, and so on. All of it lives in one JSON profile:

  1. MONGOOSE_PROFILE_FILE   explicit override, read from the real process environment only
  2. <data_dir>/profile.json  the private profile next to the brain (data_dir = MONGOOSE_DB_PATH's parent)
  3. the packaged default     src/mongoose/assets/profile.default.json, entirely fictional

Binding the profile to the brain's folder means a real brain gets the real profile and a test brain gets
the fictional one; they cannot half-swap. A private profile must be COMPLETE: missing or unknown keys
fail loud instead of silently mixing fictional values into real output. Secrets never go here (they stay
in .env); the profile holds private but non-secret identity, and the file should be readable by its owner
only (`profile_status()` reports loose permissions).

Two ways to use it:
  - typed accessors for code paths: site_domain(), wp_namespace(), block_codes(), flat_regex(), ...
  - render(text) for user-facing copy: "{{site.domain}}" style placeholders filled at serve time.
"""
from __future__ import annotations

import json
import os
import re
import stat
from functools import lru_cache
from pathlib import Path
from typing import Any

PACKAGED_PROFILE = Path(__file__).resolve().parent / "assets" / "profile.default.json"
PROFILE_VERSION = 1

# Schema: dotted key -> expected type. Lists and dicts are checked shallowly; lexicon content is free-form.
SCHEMA: dict[str, type | tuple[type, ...]] = {
    "profile_version": int,
    "owner.name": str,
    "owner.id": str,
    "owner.whatsapp_session": str,
    "site.domain": str,
    "site.public_url": str,
    "site.rtm_email": str,
    "site.home_lat": (int, float),
    "site.home_lon": (int, float),
    "rtm.name": str,
    "rtm.slug": str,
    "rtm.company_number": str,
    "rtm.payment_ref_prefix": str,
    "rtm.blocks": list,
    "wordpress.namespace": str,
    "wordpress.plugin_slug": str,
    "voicemail.sender": str,
    "voicemail.provider_token": str,
    "voicemail.ha_share_dir": str,
    "lexicons": dict,
}
REQUIRED_BLOCK_KEYS = {"code": str, "number": str, "street": str, "aliases": list}


class ProfileError(RuntimeError):
    """The private profile is missing, incomplete or malformed. Never fall back to fictional values."""


def _from_process_env(key: str) -> str | None:
    # Read the override from the real process environment. settings.py strips identity keys that only
    # came from a .env file, so no .env anywhere can repoint which profile a brain uses.
    v = os.environ.get(key)
    return v.strip() if v and v.strip() else None


_DATA_DIR_CACHE: dict[tuple, Path | None] = {}


def _data_dir() -> Path | None:
    """The brain's folder, resolved once per brain-selecting environment (settings reads .env, so it is too
    slow to consult on every accessor call). Keyed on the env vars that can move the brain."""
    key = (os.environ.get("MONGOOSE_DB_PATH"), os.environ.get("MONGOOSE_DATA_DIR"), os.environ.get("HOME"))
    if key not in _DATA_DIR_CACHE:
        try:
            from .settings import get_settings
            _DATA_DIR_CACHE[key] = Path(get_settings().data_dir)
        except Exception:
            _DATA_DIR_CACHE[key] = None
    return _DATA_DIR_CACHE[key]


def profile_path() -> Path:
    override = _from_process_env("MONGOOSE_PROFILE_FILE")
    if override:
        return Path(override).expanduser()
    d = _data_dir()
    if d is not None:
        candidate = d / "profile.json"
        if candidate.exists():
            return candidate
    return PACKAGED_PROFILE


def _get(d: dict[str, Any], dotted: str) -> Any:
    cur: Any = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(dotted)
        cur = cur[part]
    return cur


def _flatten_keys(d: dict[str, Any], prefix: str = "") -> set[str]:
    out = set()
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict) and key != "lexicons":
            out |= _flatten_keys(v, key + ".")
        else:
            out.add(key)
    return out


def validate(data: dict[str, Any], *, source: str = "profile") -> None:
    """Raise ProfileError unless `data` has exactly the schema's keys with the right types."""
    problems = []
    for key, typ in SCHEMA.items():
        try:
            val = _get(data, key)
        except KeyError:
            problems.append(f"missing {key}")
            continue
        if not isinstance(val, typ) or isinstance(val, bool) and typ is not bool:
            problems.append(f"{key} should be {typ}")
    unknown = _flatten_keys(data) - set(SCHEMA)
    if unknown:
        problems.append("unknown keys: " + ", ".join(sorted(unknown)))
    if not problems and _get(data, "profile_version") != PROFILE_VERSION:
        problems.append(f"profile_version must be {PROFILE_VERSION}")
    for i, b in enumerate(data.get("rtm", {}).get("blocks") or []):
        for k, typ in REQUIRED_BLOCK_KEYS.items():
            if not isinstance(b, dict) or not isinstance(b.get(k), typ):
                problems.append(f"rtm.blocks[{i}].{k} should be {typ.__name__}")
        if isinstance(b, dict) and not re.fullmatch(r"[0-9]+[A-Z]{1,4}", str(b.get("code", ""))):
            problems.append(f"rtm.blocks[{i}].code must look like 1EC (digits then capitals)")
    if not (data.get("rtm", {}).get("blocks")):
        problems.append("rtm.blocks must list at least one block")
    if problems:
        raise ProfileError(f"{source}: " + "; ".join(problems))


@lru_cache(maxsize=8)
def _load(path_str: str, mtime: float) -> dict[str, Any]:
    with open(path_str, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    validate(data, source=path_str)
    return data


def get_profile() -> dict[str, Any]:
    """The active profile (validated, cached until the file changes)."""
    p = profile_path()
    try:
        mtime = p.stat().st_mtime
    except OSError as exc:
        raise ProfileError(f"profile not readable: {p} ({exc})") from exc
    return _load(str(p), mtime)


def reset_cache() -> None:
    _load.cache_clear()
    _DATA_DIR_CACHE.clear()


def get(dotted: str) -> Any:
    return _get(get_profile(), dotted)


def is_default() -> bool:
    return profile_path().resolve() == PACKAGED_PROFILE.resolve()


def profile_status() -> dict[str, Any]:
    p = profile_path()
    out: dict[str, Any] = {"path": str(p), "default": is_default(), "ok": True}
    try:
        get_profile()
    except ProfileError as exc:
        out.update(ok=False, error=str(exc))
    if not out["default"]:
        try:
            mode = p.stat().st_mode
            out["private"] = not (mode & (stat.S_IRWXG | stat.S_IRWXO))
            if not out["private"]:
                out["warning"] = f"{p} is readable by other users; run: chmod 600 '{p}'"
        except OSError:
            pass
    return out


# ---------------------------------------------------------------------------------------------------------
# Typed accessors used by code paths
# ---------------------------------------------------------------------------------------------------------

def owner_name() -> str:
    return get("owner.name")


def owner_id() -> str:
    return get("owner.id")


def site_domain() -> str:
    return get("site.domain")


def public_url() -> str:
    return get("site.public_url").rstrip("/")


def rtm_email() -> str:
    return get("site.rtm_email")


def rtm_name() -> str:
    return get("rtm.name")


def rtm_slug() -> str:
    return get("rtm.slug")


def wp_namespace() -> str:
    return get("wordpress.namespace").strip("/")


def wp_route(path: str) -> str:
    """'/sync/snapshot' -> '/wp-json/<namespace>/sync/snapshot'."""
    return f"/wp-json/{wp_namespace()}/{path.lstrip('/')}"


def wp_plugin_slug() -> str:
    return get("wordpress.plugin_slug")


def blocks() -> list[dict[str, Any]]:
    return list(get("rtm.blocks"))


def block_codes() -> list[str]:
    return [b["code"] for b in blocks()]


def flat_id_regex() -> str:
    """Alternation for a canonical flat id, e.g. '(?:1EC|3EC)F\\d+'."""
    return "(?:" + "|".join(re.escape(c) for c in block_codes()) + r")F\d+"


def block_code_regex() -> str:
    return "(?:" + "|".join(re.escape(c) for c in block_codes()) + ")"


def loose_flat_regex() -> str:
    """Spoken/typed flat references: '1EC F2', '1 ec f 2', '1ecf2' (block number, letters, optional F, digit).
    Groups: (number)(flat)."""
    nums = "|".join(re.escape(b["number"]) for b in blocks())
    letters_alt = "|".join(r"\s?".join(re.escape(ch) for ch in re.sub(r"^[0-9]+", "", b["code"]).lower())
                           for b in blocks())
    return rf"\b({nums})\s?(?:{letters_alt})\s?f?\s?(\d{{1,2}})\b"


def block_for(text: str) -> str | None:
    """Map a block number, code or alias ('1', '1EC', '1 Example Road', '1EXAMPLEROAD') to its code."""
    t = re.sub(r"[^a-z0-9]", "", (text or "").lower())
    if not t:
        return None
    for b in blocks():
        keys = {b["number"], b["code"].lower()} | {re.sub(r"[^a-z0-9]", "", a.lower()) for a in b["aliases"]}
        keys.add(re.sub(r"[^a-z0-9]", "", (b["number"] + b["street"]).lower()))
        if t in keys:
            return b["code"]
    return None


def flat_id(block: str, flat_number: str | int) -> str:
    code = block_for(str(block)) or str(block).upper()
    return f"{code}F{int(str(flat_number).strip())}"


def block_address(code: str) -> str:
    for b in blocks():
        if b["code"] == code:
            return f"{b['number']} {b['street']}"
    return code


def payment_ref_prefix() -> str:
    return get("rtm.payment_ref_prefix")


def company_number() -> str:
    return get("rtm.company_number")


def voicemail_sender() -> str:
    env = _from_process_env("MONGOOSE_VOICEMAIL_SENDER")
    return (env or get("voicemail.sender")).lower()


def voicemail_provider_token() -> str:
    return get("voicemail.provider_token")


def ha_voicemail_dir() -> str:
    return get("voicemail.ha_share_dir")


def lexicon(name: str, default: Any = None) -> Any:
    """A profile lexicon extension (word lists, concept groups) merged by the caller over its base."""
    return get_profile().get("lexicons", {}).get(name, default)


# ---------------------------------------------------------------------------------------------------------
# Templating for user-facing copy
# ---------------------------------------------------------------------------------------------------------

_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+(?:\.[a-z_]+)+)\s*\}\}")


def render(text: str) -> str:
    """Fill '{{site.domain}}'-style placeholders from the profile. Unknown keys fail loud: copy must never
    ship with a raw placeholder or a silently wrong value."""
    if "{{" not in text:
        return text
    prof = get_profile()

    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key == "wordpress.rest":
            return "/wp-json/" + wp_namespace()
        try:
            val = _get(prof, key)
        except KeyError as exc:
            raise ProfileError(f"unknown profile placeholder {{{{{key}}}}}") from exc
        if isinstance(val, (dict, list)):
            raise ProfileError(f"placeholder {{{{{key}}}}} is not a scalar")
        return str(val)

    return _PLACEHOLDER.sub(sub, text)
