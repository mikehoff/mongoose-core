from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

# Unified search: one query across EVERY place a document or message can live, so you don't have to
# remember which index to look in. It searches files, attachments, email, voicemails and notifications,
# normalises them into a common item shape, ranks by match quality (not connector type), and persists a
# short-lived result set so chat/UI references such as "the first one" don't vanish on the next request.

_PRIVATE = {"high", "secret"}
_KIND_BIAS = {"attachment": 1.5, "email": 1.0, "file": 0.8, "knowledge": 0.75, "voicemail": 0.6, "notification": 0.4}
_KIND_ORDER = {"email": 0, "attachment": 1, "file": 2, "knowledge": 3, "voicemail": 4, "notification": 5}


def _mask(value: str | None, privacy: str, *, label: str) -> str:
    return f"[{label}]" if str(privacy or "").lower() in _PRIVATE else (value or "")


def _hash_ref(kind: str, *parts: Any) -> str:
    blob = "\x1f".join(str(p or "") for p in parts)
    return f"{kind[:1].upper()}{hashlib.sha1(blob.encode('utf-8')).hexdigest()[:6].upper()}"


def _tokens(q: str) -> list[str]:
    return [t for t in re.findall(r"[\w@.+-]+", (q or "").lower()) if len(t) > 1]


def _parse_date(value: Any) -> float:
    if not value:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    try:
        # Python accepts +00:00 but not a bare Z on older versions.
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _extract_correlation_keys(*values: Any) -> list[str]:
    """Tiny deterministic key extractor used for cross-domain clues.

    It is not a full entity graph, but it gives chat/UI enough stable hooks to say that two items may
    be related by sender, caller, invoice-like words, RTM/build terms, etc. We deliberately avoid saving
    full bodies here; these keys are short and metadata-like.
    """
    text = " ".join(str(v or "") for v in values).lower()
    keys: list[str] = []
    for email in re.findall(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", text):
        keys.append(f"email:{email}")
    for phone in re.findall(r"(?:\+?\d[\d\s().-]{6,}\d)", text):
        cleaned = re.sub(r"\D+", "", phone)
        if len(cleaned) >= 8:
            keys.append(f"phone:{cleaned[-10:]}")
    keywords = (
        "rtm", "service", "charge", "invoice", "demand", "tribunal", "lease", "voicemail",
        "bug", "install", "watchdog", "scheduler", "mongoose", "sillygoose", "portal", "bank",
        "bank", "agent", "building"
    )
    for kw in keywords:
        if re.search(rf"\b{re.escape(kw)}\b", text):
            keys.append(f"term:{kw}")
    # Preserve order but de-dupe.
    seen = set()
    out = []
    for k in keys:
        if k not in seen:
            seen.add(k); out.append(k)
    return out[:12]


def _match_score(q: str, item: dict[str, Any]) -> tuple[float, list[str]]:
    terms = _tokens(q)
    phrase = " ".join(terms)
    title = str(item.get("title") or "").lower()
    snippet = str(item.get("snippet") or "").lower()
    where = str(item.get("where") or "").lower()
    hay = " ".join((title, snippet, where))
    score = _KIND_BIAS.get(str(item.get("kind") or ""), 0.0)
    reasons: list[str] = []
    if not terms:
        return score, reasons
    if phrase and phrase in title:
        score += 55; reasons.append("exact phrase in title")
    elif phrase and phrase in snippet:
        score += 35; reasons.append("exact phrase in snippet")
    title_hits = sum(1 for t in terms if t in title)
    snippet_hits = sum(1 for t in terms if t in snippet)
    where_hits = sum(1 for t in terms if t in where)
    if title_hits:
        score += 12 * title_hits; reasons.append(f"{title_hits}/{len(terms)} query term(s) in title")
    if snippet_hits:
        score += 5 * snippet_hits; reasons.append(f"{snippet_hits}/{len(terms)} query term(s) in snippet")
    if where_hits:
        score += 2 * where_hits
    if terms and all(t in hay for t in terms):
        score += 18; reasons.append("all query terms present")
    # Respect each source's own BM25 order as a small within-connector signal, but never let connector
    # type dominate relevance.
    try:
        source_rank = int(item.get("source_rank") or 99)
        score += max(0, 12 - source_rank) * 0.4
    except Exception:
        pass
    date_ts = _parse_date(item.get("date") or item.get("indexed_at"))
    if date_ts:
        # A tiny freshness bump only; relevance still wins.
        age_days = max(0.0, (datetime.now(timezone.utc).timestamp() - date_ts) / 86400)
        if age_days < 30:
            score += max(0.0, 3.0 - age_days / 10.0)
    return round(score, 3), reasons[:4]


def _actions_for(item: dict[str, Any]) -> list[dict[str, str]]:
    kind = str(item.get("kind") or "")
    if kind == "file":
        return [
            {"action": "show_location", "label": "Show file location"},
            {"action": "search_nearby", "label": "Find related items"},
        ]
    if kind == "email":
        return [
            {"action": "open_email", "label": "Open email"},
            {"action": "download_email_txt", "label": "Download as text"},
            {"action": "download_email_eml", "label": "Download original .eml"},
            {"action": "search_nearby", "label": "Find related items"},
        ]
    if kind == "attachment":
        acts = [
            {"action": "open_parent_email", "label": "Open parent email"},
            {"action": "search_nearby", "label": "Find related items"},
        ]
        if item.get("downloadable"):
            acts.insert(1, {"action": "download_attachment", "label": "Download attachment"})
        return acts
    if kind == "voicemail":
        acts = [
            {"action": "open_voicemail", "label": "Open voicemail"},
            {"action": "edit_transcript", "label": "Add/edit transcript"},
            {"action": "search_nearby", "label": "Find related items"},
        ]
        if item.get("audio_attachment_id") or item.get("remote_audio_path"):
            acts.insert(1, {"action": "download_audio", "label": "Download audio"})
        return acts
    if kind == "notification":
        return [
            {"action": "view_notification", "label": "View notification"},
            {"action": "search_nearby", "label": "Find related items"},
        ]
    return [{"action": "search_nearby", "label": "Find related items"}]


def _finalise(q: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for item in items:
        item.setdefault("connector", item.get("source") or item.get("device") or item.get("kind"))
        item["score"], item["rank_reason"] = _match_score(q, item)
        item["correlation_keys"] = _extract_correlation_keys(item.get("title"), item.get("where"), item.get("snippet"))
        item["actions"] = _actions_for(item)
    items.sort(key=lambda r: (-float(r.get("score") or 0), -_parse_date(r.get("date") or r.get("indexed_at")), _KIND_ORDER.get(str(r.get("kind") or ""), 9), int(r.get("source_rank") or 99)))
    for idx, item in enumerate(items, start=1):
        item["rank"] = idx
    return items


def _file_hits(q: str, limit: int) -> list[dict[str, Any]]:
    from .file_index import search_files
    out = []
    for i, r in enumerate(search_files(q, limit), start=1):
        device = r.get("device", "local")
        path = r.get("path")
        out.append({
            "kind": "file",
            "title": r.get("title"),
            "where": f"{device}: {path}",
            "device": device,
            "connector": f"file:{device}",
            "native_id": path,
            "snippet": (r.get("snippet") or "")[:200],
            "privacy": "normal",
            "open": path,
            "date": r.get("indexed_at"),
            "indexed_at": r.get("indexed_at"),
            "source_rank": i,
            "item_ref": _hash_ref("file", device, path),
        })
    return out


def _attachment_hits(q: str, limit: int) -> list[dict[str, Any]]:
    from .attachments import search_attachments
    out = []
    for i, r in enumerate(search_attachments(q, limit), start=1):
        privacy = r.get("privacy", "normal")
        out.append({
            "kind": "attachment",
            "title": _mask(r.get("filename"), privacy, label="private attachment"),
            "where": f"email from {_mask(r.get('sender'), privacy, label='private sender')}"
                     + (f": {_mask(r.get('subject'), privacy, label='private subject')}" if r.get("subject") else ""),
            "device": "email",
            "connector": r.get("source") or "email_attachment",
            "native_id": r.get("id"),
            "snippet": "" if str(privacy).lower() in _PRIVATE else f"{r.get('mime_type') or ''} {r.get('size_bytes') or ''}b".strip(),
            "privacy": privacy,
            "open": r.get("source_message_id"),
            "content_indexed": bool(r.get("content_indexed")),
            "attachment_id": r.get("id"),
            "downloadable": bool(r.get("downloadable")),
            "email_id": r.get("email_id"),
            "filename": _mask(r.get("filename"), privacy, label="private attachment"),
            "date": r.get("email_date"),
            "source_rank": i,
            "item_ref": _hash_ref("attachment", r.get("source"), r.get("id"), r.get("source_message_id")),
        })
    return out


def _email_hits(q: str, limit: int) -> list[dict[str, Any]]:
    from .email_index import search_email
    out = []
    for i, r in enumerate(search_email(q, limit), start=1):
        privacy = r.get("privacy", "normal")
        is_private = str(privacy).lower() in _PRIVATE
        out.append({
            "kind": "email",
            "title": _mask(r.get("subject") or "(no subject)", privacy, label="private email"),
            "where": f"{r.get('source') or 'email'}/{r.get('mailbox') or ''}"
                     + (f" from {r.get('sender')}" if not is_private and r.get("sender") else ""),
            "device": "email",
            "connector": r.get("source") or "email",
            "native_id": r.get("id"),
            "snippet": "" if is_private else (r.get("snippet") or "")[:200],
            "privacy": privacy,
            "open": r.get("id"),
            "date": r.get("received_at") or r.get("sent_at"),
            "source_rank": i,
            "item_ref": _hash_ref("email", r.get("source"), r.get("id")),
        })
    return out


def _voicemail_hits(q: str, limit: int) -> list[dict[str, Any]]:
    from .voicemail import search_voicemails
    out = []
    for i, r in enumerate(search_voicemails(q, limit), start=1):
        privacy = r.get("privacy", "normal")
        is_private = str(privacy).lower() in _PRIVATE
        caller = r.get("caller_number") or "unknown caller"
        body = (r.get("transcript") or r.get("notice_text") or "")
        out.append({
            "kind": "voicemail",
            "title": _mask(r.get("subject") or f"Voicemail from {caller}", privacy, label="private voicemail"),
            "where": f"{r.get('provider') or 'voicemail'}" + (f" from {caller}" if not is_private else ""),
            "device": "voicemail",
            "connector": r.get("source") or r.get("provider") or "voicemail",
            "native_id": r.get("id"),
            "snippet": "" if is_private else body[:200],
            "privacy": privacy,
            "open": r.get("id"),
            "date": r.get("voicemail_at") or r.get("email_date") or r.get("indexed_at"),
            "email_id": r.get("email_id"),
            "audio_attachment_id": r.get("audio_attachment_id"),
            "remote_audio_path": r.get("remote_audio_path"),
            "source_rank": i,
            "item_ref": _hash_ref("voicemail", r.get("identity_key") or r.get("id")),
        })
    return out


def _notification_hits(q: str, limit: int) -> list[dict[str, Any]]:
    from .notifications import search_notifications
    out = []
    for i, r in enumerate(search_notifications(q, limit), start=1):
        privacy = r.get("privacy", "normal")
        is_private = str(privacy).lower() in _PRIVATE
        out.append({
            "kind": "notification",
            "title": _mask(r.get("title") or "(notification)", privacy, label="private notification"),
            "where": f"{r.get('app') or r.get('source') or 'notification'}",
            "device": r.get("device") or "notification",
            "connector": r.get("source") or "notification",
            "native_id": r.get("id"),
            "snippet": "" if is_private else (r.get("body") or "")[:200],
            "privacy": privacy,
            "open": r.get("id"),
            "date": r.get("received_at"),
            "source_rank": i,
            "item_ref": _hash_ref("notification", r.get("source"), r.get("id")),
        })
    return out


def _knowledge_hits(q: str, limit: int) -> list[dict[str, Any]]:
    """Imported knowledge (claims, insights, memories, entities, care plans) as unified find hits."""
    try:
        from ..knowledge_import.search import search as search_knowledge
        return search_knowledge(q, limit=limit)
    except Exception:
        return []


def find(q: str, *, limit: int = 10, kinds: tuple[str, ...] | None = None,
         persist: bool = True, surface: str = "search") -> dict[str, Any]:
    """Search all item-like stores and return a ranked, actionable, persisted result set."""
    want = set(kinds) if kinds else {"file", "attachment", "email", "voicemail", "notification", "knowledge"}
    results: list[dict[str, Any]] = []
    per = max(limit * 2, 10)
    if "file" in want:
        results.extend(_file_hits(q, per))
    if "attachment" in want:
        results.extend(_attachment_hits(q, per))
    if "email" in want:
        results.extend(_email_hits(q, per))
    if "voicemail" in want:
        results.extend(_voicemail_hits(q, per))
    if "notification" in want:
        results.extend(_notification_hits(q, per))
    if "knowledge" in want:
        results.extend(_knowledge_hits(q, per))
    results = _finalise(q, results)[:limit]

    def _count(sql: str) -> int:
        try:
            from ..db import connect
            with connect() as _con:
                try:
                    return int(_con.execute(sql).fetchone()[0])
                except Exception:
                    return 0
        except Exception:
            return 0

    files_indexed = _count("SELECT COUNT(*) FROM files")
    email_indexed = _count("SELECT COUNT(*) FROM email_messages")
    attach_indexed = _count("SELECT COUNT(*) FROM attachments")
    vm_indexed = _count("SELECT COUNT(*) FROM voicemail_messages")
    notif_indexed = _count("SELECT COUNT(*) FROM notifications")
    knowledge_indexed = _count("SELECT COUNT(*) FROM knowledge_claims")
    counts = {"file": files_indexed, "email": email_indexed, "attachment": attach_indexed,
              "voicemail": vm_indexed, "notification": notif_indexed, "knowledge": knowledge_indexed}
    searched_total = sum(counts[k] for k in want if k in counts)
    result_set = None
    if persist and results:
        try:
            from .item_model import remember_search_results
            result_set = remember_search_results(q, results, surface=surface)
            for r in results:
                r["result_set_id"] = result_set["result_set_id"]
        except Exception:
            result_set = None
    return {"query": q, "count": len(results), "results": results,
            "result_set_id": result_set.get("result_set_id") if result_set else None,
            "result_set_expires_at": result_set.get("expires_at") if result_set else None,
            "searched": sorted(want),
            "indexed_count": files_indexed,
            "indexed_counts": {"files": files_indexed, "email": email_indexed,
                               "attachments": attach_indexed, "voicemail": vm_indexed,
                               "notifications": notif_indexed},
            "empty_index": searched_total == 0}


def find_export(q: str, *, limit: int = 50, kinds: tuple[str, ...] | None = None,
                fmt: str = "markdown") -> dict[str, Any]:
    """Run a unified search and render the results as a portable export."""
    res = find(q, limit=limit, kinds=kinds, persist=False)
    results = res.get("results", [])
    if fmt == "json":
        import json as _json
        return {"ok": True, "format": "json", "query": q, "count": len(results),
                "content": _json.dumps(res, indent=2, default=str)}

    lines = [f"# Search export: “{q}”", ""]
    lines.append(f"_{len(results)} result(s) across {', '.join(res.get('searched', []))}._")
    lines.append("")
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        by_kind.setdefault(r.get("kind", "other"), []).append(r)
    kind_titles = {"file": "Files", "attachment": "Attachments", "email": "Email",
                   "voicemail": "Voicemails", "notification": "Notifications"}
    for kind in ("file", "attachment", "email", "voicemail", "notification"):
        hits = by_kind.get(kind)
        if not hits:
            continue
        lines.append(f"## {kind_titles.get(kind, kind.title())} ({len(hits)})")
        lines.append("")
        for h in hits:
            title = h.get("title") or "(untitled)"
            where = h.get("where") or ""
            date = h.get("date") or ""
            ref = h.get("item_ref") or ""
            lines.append(f"- **{title}**" + (f": {where}" if where else "") + (f"  _{date}_" if date else "") + (f"  `{ref}`" if ref else ""))
            snip = h.get("snippet")
            if snip:
                lines.append(f"  > {snip}")
            acts = ", ".join(a.get("label", a.get("action", "")) for a in h.get("actions", [])[:4])
            if acts:
                lines.append(f"  Actions: {acts}")
        lines.append("")
    content = "\n".join(lines)
    return {"ok": True, "format": "markdown", "query": q, "count": len(results), "content": content}
