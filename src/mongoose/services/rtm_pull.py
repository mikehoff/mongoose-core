"""Pull RTM documents from the site's RTM WordPress plugin into Mongoose.

Mongoose is local-first behind CGNAT, so it PULLS rather than being pushed to: it polls the plugin's
document queue, ingests each pending document through rtm_docs.ingest (storage + OCR + global search +
correlation), then acks them so they are never pulled twice. Auditor notes are pulled the same way.

This reuses the EXISTING signed portal channel (rtm_portal._wp_call): the same WordPress basic auth
plus HMAC-SHA256 body signature (X-Mongoose-Signature, secret MONGOOSE_WEBHOOK_SECRET) that already
protects the plugin's <namespace>/sync routes. It does NOT invent a new secret or header. Reads (GET) go
through the plugin's admin permission; writes (ack POST) are HMAC-signed. Everything is outbound and a
clean no-op with a clear message until WordPress is configured, so it is safe to ship before the
plugin side exists.

Contract (must match RTM_DOCUMENT_INTAKE_SPEC.md in the plugin source; <namespace> is the site profile's
wordpress.namespace, resolved at call time):
  GET  /wp-json/<namespace>/documents/pending?limit=N
       -> {documents:[{id, doc_type, flat_id, filename, mime, size_bytes, content_b64,
                       uploaded_by, role, note, created_at}]}
  POST /wp-json/<namespace>/documents/ack  {ids:[...]} -> {ok, consumed}
  GET  /wp-json/<namespace>/documents/notes/pending  (+ notes/ack), for auditor notes.
"""
from __future__ import annotations

import base64
from typing import Any

from .. import profile as _profile
from ..db import audit

# Map whatever the plugin sends (an RTM role, or a raw WordPress role slug) onto our RTM roles. The
# plugin should already send the mapped RTM role, but Mongoose normalises defensively. Unknown roles
# fall back to leaseholder: the least-privileged role that may still upload its own flat's evidence.
_WP_ROLE_MAP = {
    "rtm_director": "director", "director": "director",
    "rtm_admin": "admin", "admin": "admin", "administrator": "admin",
    "rtm_leaseholder": "leaseholder", "leaseholder": "leaseholder", "subscriber": "leaseholder",
    "rtm_auditor": "auditor", "auditor": "auditor",
}


def map_role(wp_role: str) -> str:
    return _WP_ROLE_MAP.get((wp_role or "").strip().lower(), "leaseholder")


def is_configured() -> bool:
    """True when WordPress is connected (the puller can reach the plugin)."""
    try:
        from .rtm_portal import _wp_base
        return bool(_wp_base())
    except Exception:
        return False


def pull_now(limit: int = 20) -> dict[str, Any]:
    """Pull, ingest, and ack pending RTM documents (and auditor notes) from the plugin queue."""
    from .rtm_portal import _wp_call
    from .rtm_docs import ingest

    res = _wp_call("GET", _profile.wp_route(f"/documents/pending?limit={int(limit)}"))
    if not res.get("ok"):
        # 404 here means the plugin has no documents routes yet (build it first); other errors bubble up.
        return {"ok": False, "error": res.get("error", "could not reach plugin document queue")}
    docs = (res.get("data") or {}).get("documents", []) or []

    ingested, rejected = 0, 0
    done_ids: list[Any] = []
    for d in docs:
        payload = _decode_document(d)
        if payload is None:
            # Bad/undecryptable payload is a permanent verdict, not transient: ack so it stops
            # blocking the queue.
            rejected += 1
            done_ids.append(d.get("id"))
            continue
        out = ingest(content=payload["content"], filename=payload["filename"],
                     doc_type=str(d.get("doc_type") or ""),
                     flat_id=(str(d.get("flat_id")).strip() or None),
                     uploaded_by=str(d.get("uploaded_by") or "wordpress"),
                     role=map_role(str(d.get("role") or "")),
                     source="wordpress", note=payload["note"])
        if out.get("ok"):
            ingested += 1
        else:
            rejected += 1
        # Either way the verdict is definitive (ingest always returns), so ack to clear the queue.
        done_ids.append(d.get("id"))

    acked = _ack(_profile.wp_route("/documents/ack"), done_ids)
    notes_added = _pull_notes()
    audit("rtm", "documents_pulled", True, route="rtm_pull",
          payload={"ingested": ingested, "rejected": rejected, "acked": acked, "notes": notes_added})
    return {"ok": True, "pulled": len(docs), "ingested": ingested, "rejected": rejected,
            "acked": acked, "notes_added": notes_added}


def _decode_document(d: dict) -> dict[str, Any] | None:
    """Extract {content: bytes, filename, note} from a queue row.

    Prefers a NaCl sealed-box encrypted payload (`encrypted_b64`): this is the REQUIRED transport,
    because the portal security rules forbid raw documents, raw payment references, or document numbers
    ever resting in WordPress in the clear. The plugin seals a JSON {filename, mime, content_b64, note}
    to Mongoose's published claim public key; we decrypt it locally with the private key that never
    leaves the Mac (reusing rtm_portal.decrypt_claim_payload). A clear `content_b64` is accepted only as
    a local/testing fallback and should never be used for real WordPress traffic."""
    enc = d.get("encrypted_b64") or d.get("encrypted_payload")
    if enc:
        try:
            from .rtm_portal import decrypt_claim_payload
            dec = decrypt_claim_payload(str(enc))
            if not dec.get("ok"):
                return None
            p = dec.get("claim") or {}
            return {"content": base64.b64decode((p.get("content_b64") or "").split(",", 1)[-1]),
                    "filename": str(p.get("filename") or d.get("filename") or "document"),
                    "note": str(p.get("note") or "")}
        except Exception:
            return None
    try:
        return {"content": base64.b64decode((d.get("content_b64") or "").split(",", 1)[-1]),
                "filename": str(d.get("filename") or "document"),
                "note": str(d.get("note") or "")}
    except Exception:
        return None


def _pull_notes() -> int:
    """Pull auditor notes (read-only reviewers annotate without uploading) and record them."""
    from .rtm_portal import _wp_call
    from .rtm_docs import add_note
    res = _wp_call("GET", _profile.wp_route("/documents/notes/pending"))
    if not res.get("ok"):
        return 0
    notes = (res.get("data") or {}).get("notes", []) or []
    added, done = 0, []
    for n in notes:
        try:
            out = add_note(doc_id=int(n.get("doc_ref") or 0), text=str(n.get("text") or ""),
                           author=str(n.get("author") or "wordpress"),
                           role=map_role(str(n.get("role") or "auditor")))
            if out.get("ok"):
                added += 1
            done.append(n.get("id"))
        except Exception:
            done.append(n.get("id"))
    _ack(_profile.wp_route("/documents/notes/ack"), done)
    return added


def _ack(path: str, ids: list) -> int:
    from .rtm_portal import _wp_call
    ids = [i for i in ids if i is not None]
    if not ids:
        return 0
    res = _wp_call("POST", path, json_body={"ids": ids})
    if not res.get("ok"):
        return 0
    return int((res.get("data") or {}).get("consumed", len(ids)) or 0)
