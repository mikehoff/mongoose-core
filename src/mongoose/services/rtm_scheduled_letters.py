"""Letters scheduled to be emailed at a set time (for example, nine clause 10.7 notices at 9am).

A batch is a list of letters, each a PDF already rendered for one flat, with the time to send. It is sent by
the portal through rtm_portal.send_attached_letter, so every letter goes from info@example.org, attached
from the flat-gated store, with the audit Bcc and the Sent mail log.

Safety:
  - A batch sends only when ARMED. Scheduling and arming are separate, so a batch can be prepared and read
    before anything is committed to go.
  - Each letter is sent at most once: its outcome is recorded before the next is attempted, and a letter that
    already went is never sent again, even if the job runs twice.
  - A batch that falls due while the Mac is asleep is not sent hours later at an odd time: past
    LATE_LIMIT it is held and the owner is notified instead.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..db import get_app_state, set_app_state, utc_now_iso

_KEY = "rtm_scheduled_letters_v1"
LONDON = ZoneInfo("Europe/London")
LATE_LIMIT = timedelta(hours=6)


def _load() -> list[dict[str, Any]]:
    try:
        data = json.loads(get_app_state(_KEY, "") or "[]")
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(batches: list[dict[str, Any]]) -> None:
    set_app_state(_KEY, json.dumps(batches, ensure_ascii=False))


def _parse(when: str) -> datetime:
    """An ISO time; a time without a zone is London time (what the owner means by '9am')."""
    dt = datetime.fromisoformat(str(when))
    return dt if dt.tzinfo else dt.replace(tzinfo=LONDON)


def schedule(letters: list[dict[str, Any]], send_at: str, *, label: str = "", armed: bool = False) -> dict[str, Any]:
    """Queue a batch. Each letter: flat_id, pdf_path, subject, document_number, attachment_name."""
    if not letters:
        return {"ok": False, "error": "no letters"}
    for x in letters:
        missing = [k for k in ("flat_id", "pdf_path", "subject", "document_number") if not x.get(k)]
        if missing:
            return {"ok": False, "error": f"letter for {x.get('flat_id') or '?'} is missing {', '.join(missing)}"}
        if not Path(x["pdf_path"]).expanduser().exists():
            return {"ok": False, "error": f"PDF not found for {x['flat_id']}: {x['pdf_path']}"}
    when = _parse(send_at)
    batch = {"id": "batch-" + secrets.token_hex(4), "label": label, "send_at": when.isoformat(),
             "armed": bool(armed), "created_at": utc_now_iso(), "state": "scheduled",
             "letters": [{**x, "status": "pending"} for x in letters]}
    batches = _load()
    batches.append(batch)
    _save(batches)
    return {"ok": True, "batch": batch["id"], "send_at": batch["send_at"], "armed": batch["armed"],
            "letters": len(letters)}


def _set(batch_id: str, **changes: Any) -> dict[str, Any]:
    batches = _load()
    for b in batches:
        if b["id"] == batch_id:
            if b["state"] in ("sent", "partly_sent", "cancelled"):
                return {"ok": False, "error": f"batch is already {b['state']}"}
            b.update(changes)
            _save(batches)
            return {"ok": True, "batch": batch_id, **changes}
    return {"ok": False, "error": f"no batch {batch_id}"}


def arm(batch_id: str) -> dict[str, Any]:
    return _set(batch_id, armed=True)


def cancel(batch_id: str) -> dict[str, Any]:
    return _set(batch_id, armed=False, state="cancelled")


def status() -> list[dict[str, Any]]:
    return [{"id": b["id"], "label": b.get("label", ""), "send_at": b["send_at"], "armed": b["armed"],
             "state": b["state"], "letters": [(x["flat_id"], x["status"]) for x in b["letters"]]}
            for b in _load()]


def run_due(now: datetime | None = None, *, sender=None, notify=None) -> dict[str, Any]:
    """Send every armed batch whose time has come. Called each minute by the scheduler."""
    now = now or datetime.now(LONDON)
    if sender is None:
        from .rtm_portal import send_attached_letter as sender
    out: dict[str, Any] = {"ok": True, "sent": 0, "failed": 0, "held": 0}
    batches = _load()
    for b in batches:
        if not b.get("armed") or b.get("state") not in ("scheduled", "sending"):
            continue
        when = _parse(b["send_at"])
        if now < when:
            continue
        if now - when > LATE_LIMIT and b["state"] == "scheduled":
            b["state"], b["armed"] = "held_late", False
            _save(batches)
            out["held"] += 1
            if notify:
                notify(f"Letters not sent: {b.get('label') or b['id']}",
                       f"They were due at {when:%H:%M on %d %B} but Mongoose was not running then. They have "
                       "been held, not sent late. Re-arm with a new time to send them.")
            continue
        b["state"] = "sending"
        _save(batches)
        for x in b["letters"]:
            if x["status"] != "pending":
                continue          # never send the same letter twice
            x["status"] = "sending"
            _save(batches)
            try:
                r = sender(x["flat_id"], x["pdf_path"], subject=x["subject"], document_number=x["document_number"],
                           attachment_name=x.get("attachment_name", ""), confirm=True)
            except Exception as exc:
                r = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            x["status"] = "sent" if r.get("ok") else "failed"
            x["result"] = {k: r.get(k) for k in ("email", "attached", "cc", "error")}
            x["at"] = utc_now_iso()
            _save(batches)
            out["sent" if r.get("ok") else "failed"] += 1
        failed = [x["flat_id"] for x in b["letters"] if x["status"] == "failed"]
        b["state"] = "partly_sent" if failed else "sent"
        _save(batches)
        if notify:
            done = sum(1 for x in b["letters"] if x["status"] == "sent")
            notify(f"Letters sent: {b.get('label') or b['id']}",
                   f"{done} of {len(b['letters'])} emailed." + (f" Failed: {', '.join(failed)}." if failed else ""))
    out["ok"] = out["failed"] == 0
    return out
