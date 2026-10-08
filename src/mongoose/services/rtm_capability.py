from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

# RTM capability + build-candidate state.
#
# Answers four questions the operator (and the scheduler) keep asking:
#   1. What can RTM actually DO right now? (capability state , R1-R7, derived from real data, not
#      from what a plan claims.)
#   2. How is the RTM PRODUCT build progressing? (build candidates , possibly several competing
#      approaches to the same goal.)
#   3. Can it be iterated ad-hoc without colliding with the weekly/daily scheduled build job?
#      (a build lock: whoever holds it builds; the other defers cleanly.)
#   4. Can there be different candidates that get built and later reconciled or pruned?
#      (candidates carry a goal, a status, a lineage, and explicit promote/reconcile/prune.)
#
# Capability state is DERIVED (re-read from the live RTM data each time), never a cache that can lie.
# Build-candidate state is PERSISTED (in app_state, JSON) because it tracks intent over time.

_CANDIDATES_KEY = "rtm_build_candidates"
_LOCK_KEY = "rtm_build_lock"
_LOCK_TTL_SECONDS = 3 * 60 * 60  # a build holds the lock at most ~3h, then it's considered stale


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ts() -> float:
    return time.time()


# --- capability state (DERIVED from live RTM data) ---------------------------

def capability_state() -> dict[str, Any]:
    """What RTM can do right now, per requirement R1-R7, derived from the live data, so it can't
    drift from reality. Each requirement reports a state (one of: not_started, scaffolded, in_use,
    verified) with the evidence behind it. 'in_use' means there's real data flowing through it;
    'verified' means the real-data caveats for it are cleared."""
    caps: list[dict[str, Any]] = []
    try:
        from . import rtm_management as M
        flats = M.list_flats()
        demands = M.list_demands(limit=10000)
        payables = M.list_payables(limit=10000)
        try:
            evidence = M.list_evidence()
        except Exception:
            evidence = []
        try:
            ba = M.budget_actuals()
            budget_lines = ba.get("lines", []) if isinstance(ba, dict) else []
        except Exception:
            budget_lines = []

        real_flats = [f for f in flats if f.get("leaseholder_name")]
        any_payment = any(d.get("allocated") not in (None, "", "\u00a30.00") for d in demands)

        # R1 demand register
        caps.append(_cap("R1", "Demand register",
                         _level(bool(demands), in_use=bool(demands), verified=len(demands) >= 18),
                         f"{len(demands)} demand(s) recorded",
                         {"demands": len(demands)}))
        # R2 Metro reconciliation , 'in_use' once a payment has been allocated
        caps.append(_cap("R2", "Metro reconciliation",
                         _level(any_payment, in_use=any_payment, verified=False),
                         "payments allocated against demands" if any_payment
                         else "no bank payments allocated yet (needs a real Metro CSV + references)",
                         {}))
        # R3 arrears + chasing
        try:
            arr = M.arrears()
            arr_count = arr.get("count", 0)
        except Exception:
            arr_count = 0
        caps.append(_cap("R3", "Arrears + chasing",
                         _level(bool(demands), in_use=bool(demands), verified=False),
                         f"{arr_count} flat(s) currently in arrears" if demands
                         else "no demands to age yet",
                         {"in_arrears": arr_count}))
        # R4 per-flat trust ledger
        caps.append(_cap("R4", "Per-flat trust ledger",
                         _level(bool(real_flats), in_use=bool(demands), verified=len(real_flats) >= 18),
                         f"{len(real_flats)}/{len(flats) or 18} flats have real leaseholder data",
                         {"flats_with_data": len(real_flats), "flats_total": len(flats)}))
        # R5 payables + cashflow
        caps.append(_cap("R5", "Payables + cashflow",
                         _level(bool(payables), in_use=bool(payables), verified=False),
                         f"{len(payables)} payable(s) recorded",
                         {"payables": len(payables)}))
        # R6 budget vs actuals
        caps.append(_cap("R6", "Budget vs actuals",
                         _level(bool(budget_lines), in_use=bool(budget_lines), verified=False),
                         f"{len(budget_lines)} budget line(s) set",
                         {"budget_lines": len(budget_lines)}))
        # R7 evidence store
        caps.append(_cap("R7", "Evidence store",
                         _level(bool(evidence), in_use=bool(evidence), verified=False),
                         f"{len(evidence)} evidence item(s) attached",
                         {"evidence": len(evidence)}))
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200], "requirements": []}

    in_use = sum(1 for c in caps if c["state"] in ("in_use", "verified"))
    return {"ok": True, "as_at": _now_iso(),
            "requirements": caps,
            "summary": f"{in_use}/{len(caps)} requirements in use",
            "in_use": in_use, "total": len(caps)}


def _level(exists: bool, *, in_use: bool, verified: bool) -> str:
    if verified:
        return "verified"
    if in_use:
        return "in_use"
    if exists:
        return "scaffolded"
    return "not_started"


def _cap(req: str, label: str, state: str, why: str, counts: dict[str, Any]) -> dict[str, Any]:
    return {"requirement": req, "label": label, "state": state, "why": why, "counts": counts}


# --- build candidates (PERSISTED intent over time) ---------------------------

def _load_candidates() -> list[dict[str, Any]]:
    from ..db import get_app_state
    raw = get_app_state(_CANDIDATES_KEY, "")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def _save_candidates(cands: list[dict[str, Any]]) -> None:
    from ..db import set_app_state
    set_app_state(_CANDIDATES_KEY, json.dumps(cands))


def create_candidate(goal: str, *, approach: str = "", parent: str = "",
                     plan_id: str = "", origin: str = "adhoc") -> dict[str, Any]:
    """Register a build candidate, one approach to a build goal. Several candidates can target the
    same goal (e.g. two different ways to build the WordPress admin surface); they're reconciled or
    pruned later. `origin` is 'adhoc' (operator/Claude started it now) or 'scheduled' (a cadence job
    created it). `parent` links a candidate derived from another (lineage)."""
    cands = _load_candidates()
    cid = "cand_" + uuid.uuid4().hex[:10]
    cand = {"id": cid, "goal": goal, "approach": approach, "parent": parent or None,
            "plan_id": plan_id or None, "origin": origin, "status": "proposed",
            "created_at": _now_iso(), "updated_at": _now_iso(), "notes": []}
    cands.append(cand)
    _save_candidates(cands)
    return cand


def list_candidates(goal: str | None = None, *, status: str | None = None) -> list[dict[str, Any]]:
    cands = _load_candidates()
    if goal:
        cands = [c for c in cands if c.get("goal") == goal]
    if status:
        cands = [c for c in cands if c.get("status") == status]
    return cands


def update_candidate(cid: str, **changes: Any) -> dict[str, Any]:
    cands = _load_candidates()
    for c in cands:
        if c["id"] == cid:
            note = changes.pop("note", None)
            c.update(changes)
            if note:
                c.setdefault("notes", []).append({"at": _now_iso(), "note": note})
            c["updated_at"] = _now_iso()
            _save_candidates(cands)
            return {"ok": True, "candidate": c}
    return {"ok": False, "reason": "candidate_not_found", "id": cid}


def promote_candidate(cid: str) -> dict[str, Any]:
    """Promote one candidate to 'chosen' for its goal; any sibling candidates for the same goal are
    marked 'superseded' (kept for lineage, not pruned). This is the reconcile step: pick the winner,
    keep the losers' history."""
    cands = _load_candidates()
    target = next((c for c in cands if c["id"] == cid), None)
    if not target:
        return {"ok": False, "reason": "candidate_not_found", "id": cid}
    goal = target["goal"]
    superseded = []
    for c in cands:
        if c["goal"] == goal and c["id"] != cid and c["status"] not in ("pruned", "superseded"):
            c["status"] = "superseded"
            c["updated_at"] = _now_iso()
            superseded.append(c["id"])
    target["status"] = "chosen"
    target["updated_at"] = _now_iso()
    _save_candidates(cands)
    return {"ok": True, "chosen": cid, "superseded": superseded, "goal": goal}


def prune_candidates(*, status: str = "superseded", goal: str | None = None) -> dict[str, Any]:
    """Prune candidates by status (default: the superseded ones), optionally scoped to a goal. This
    is the cleanup step, remove dead approaches once a winner is settled. 'chosen' and 'building'
    candidates are never pruned by this call."""
    cands = _load_candidates()
    keep, pruned = [], []
    for c in cands:
        is_match = (c.get("status") == status) and (goal is None or c.get("goal") == goal)
        if is_match and c.get("status") not in ("chosen", "building"):
            pruned.append(c["id"])
        else:
            keep.append(c)
    _save_candidates(keep)
    return {"ok": True, "pruned": pruned, "remaining": len(keep)}


# --- the build lock (ad-hoc vs scheduled , never collide) --------------------

def acquire_build_lock(holder: str) -> dict[str, Any]:
    """Try to take the RTM build lock. Returns {ok:True, token} if acquired, or {ok:False, held_by}
    if someone else holds a fresh lock. A stale lock (older than the TTL) is broken automatically, a
    crashed build never wedges the system. `holder` is a label like 'adhoc:owner' or 'scheduled:daily'."""
    from ..db import get_app_state, set_app_state
    raw = get_app_state(_LOCK_KEY, "")
    if raw:
        try:
            cur = json.loads(raw)
            if _now_ts() - cur.get("ts", 0) < _LOCK_TTL_SECONDS:
                return {"ok": False, "held_by": cur.get("holder"), "since": cur.get("at")}
        except Exception:
            pass  # corrupt lock -> treat as free
    token = uuid.uuid4().hex
    set_app_state(_LOCK_KEY, json.dumps({"holder": holder, "token": token,
                                         "ts": _now_ts(), "at": _now_iso()}))
    return {"ok": True, "token": token, "holder": holder}


def release_build_lock(token: str) -> dict[str, Any]:
    """Release the lock if the token matches (so one holder can't release another's lock)."""
    from ..db import get_app_state, set_app_state
    raw = get_app_state(_LOCK_KEY, "")
    if not raw:
        return {"ok": True, "note": "no lock held"}
    try:
        cur = json.loads(raw)
    except Exception:
        set_app_state(_LOCK_KEY, "")
        return {"ok": True, "note": "cleared corrupt lock"}
    if cur.get("token") != token:
        return {"ok": False, "reason": "token_mismatch", "held_by": cur.get("holder")}
    set_app_state(_LOCK_KEY, "")
    return {"ok": True, "released": True}


def build_lock_status() -> dict[str, Any]:
    from ..db import get_app_state
    raw = get_app_state(_LOCK_KEY, "")
    if not raw:
        return {"held": False}
    try:
        cur = json.loads(raw)
    except Exception:
        return {"held": False, "note": "corrupt lock record"}
    stale = _now_ts() - cur.get("ts", 0) >= _LOCK_TTL_SECONDS
    return {"held": not stale, "holder": cur.get("holder"), "since": cur.get("at"), "stale": stale}


# --- combined state (one object the dashboard / readiness / brain can read) ---

def rtm_state() -> dict[str, Any]:
    """One object: current capability state + build candidates + lock state. This is the robust,
    queryable representation of 'where RTM is', both what it can do and what's being built."""
    cands = _load_candidates()
    by_status: dict[str, int] = {}
    for c in cands:
        by_status[c["status"]] = by_status.get(c["status"], 0) + 1
    return {"capability": capability_state(),
            "build": {"candidates": cands, "by_status": by_status,
                      "lock": build_lock_status()}}
