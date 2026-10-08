"""Entity matching, one mechanism every matcher inherits.

The failure it exists to prevent: a handler tries to pull a name (a leaseholder, a payee, a model) out of a
free message by STRIPPING the command words it knows and treating whatever survives as the name. That
denylist approach turned "patil and o'brien ... payments landed so can you mark them paid ..." into one
bogus 15-word leaseholder that matched nobody.

The fix is an allowlist: match the message against the REAL roster of that entity type, return every actual
entity it names, and ignore everything else. This is the entity-resolution twin of the grounding firewall,
match against real data, never guess, and it is inherited, not re-implemented per handler.

A subclass supplies roster(); the base owns match(). To add a new matcher (payees, models, contacts,
connectors), subclass EntityMatcher and return its real entities. That is the whole contract.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


def _norm(text: str) -> str:
    """Lowercase, alphanumeric only, so apostrophes/punctuation cannot hide a name (o'brien -> obrien)."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _word_set(text: str) -> set[str]:
    """The message as a set of normalised WORDS, plus a depluralised form of each. Word-level (not substring)
    matching is what makes short tokens safe: 'jo' matches the word 'jo' but not the word 'came', and
    'patils' still resolves to 'patil'."""
    words = {_norm(w) for w in re.split(r"[^a-z0-9']+", (text or "").lower())}
    words.discard("")
    words |= {w[:-1] for w in list(words) if w.endswith("s") and len(w) > 2}
    return words


def tokens_for(name: str, *, min_len: int = 5) -> list[str]:
    """The distinctive match tokens of a display name: its words normalised, kept only if reasonably
    distinctive (>= min_len). Short/common tokens are dropped so a financial action is never triggered by
    an incidental word."""
    out: list[str] = []
    for word in re.split(r"[^a-z']+", (name or "").lower()):
        tok = _norm(word)
        if len(tok) >= min_len and tok not in out:
            out.append(tok)
    return out


@dataclass
class Entity:
    id: str
    label: str
    tokens: list[str] = field(default_factory=list)


class EntityMatcher(ABC):
    """The inherited mechanism. Subclass and implement roster(); do not override match()."""

    min_token_len: int = 5

    @abstractmethod
    def roster(self) -> list[Entity]:
        """The REAL entities of this type, each with distinctive match tokens. May be empty."""

    def match(self, text: str) -> list[Entity]:
        """Every real entity the message names (allowlist), de-duplicated, order-preserving. Word-level, so
        it never guesses and short tokens stay safe: a message with no roster word in it returns []."""
        words = _word_set(text)
        if not words:
            return []
        found: dict[str, Entity] = {}
        for e in self.roster():
            for tok in e.tokens:
                if len(tok) >= self.min_token_len and tok in words and e.id not in found:
                    found[e.id] = e
                    break
        return list(found.values())

    def match_one(self, text: str) -> "Entity | None":
        hits = self.match(text)
        return hits[0] if len(hits) == 1 else None


class LeaseholderMatcher(EntityMatcher):
    """Match against the real Example Court leaseholder roster (rtm_mgmt_flats)."""

    def roster(self) -> list[Entity]:
        try:
            from ..db import connect
            with connect() as con:
                rows = con.execute("SELECT flat_id, leaseholder_name FROM rtm_mgmt_flats").fetchall()
        except Exception:
            return []
        out: list[Entity] = []
        for r in rows:
            fid = r[0] if isinstance(r, tuple) else r["flat_id"]
            name = (r[1] if isinstance(r, tuple) else r["leaseholder_name"]) or ""
            out.append(Entity(id=str(fid), label=name, tokens=tokens_for(name, min_len=self.min_token_len)))
        return out


# Company suffixes that are not distinctive: "Ltd" must not match every limited company.
_CORP_STOP = {"ltd", "limited", "plc", "llp", "llc", "inc", "company", "group", "trust", "services"}


class RoutineMatcher(EntityMatcher):
    """Match against the fixed routine/ritual roster (mirrors tools._tool_ritual)."""

    min_token_len = 4
    _ROUTINES = ("physio", "proprioception", "breathing", "journal", "resilience", "friend_checkin")

    def roster(self) -> list[Entity]:
        return [Entity(id=r, label=r.replace("_", " "),
                       tokens=tokens_for(r.replace("_", " "), min_len=self.min_token_len))
                for r in self._ROUTINES]


class PayeeMatcher(EntityMatcher):
    """Match against the real supplier/payee roster (rtm_payee_bank + rtm_mgmt_payables). Currently
    self-populating, so it may be empty until payees are confirmed or invoices read; an empty roster
    correctly matches nothing rather than guessing."""

    min_token_len = 3   # supplier acronyms like ISE are short

    def roster(self) -> list[Entity]:
        names: set[str] = set()
        try:
            from ..db import connect
            with connect() as con:
                for table, col in (("rtm_payee_bank", "payee_name"), ("rtm_mgmt_payables", "supplier")):
                    try:
                        for row in con.execute(f"SELECT DISTINCT {col} FROM {table}").fetchall():
                            val = row[0] if isinstance(row, tuple) else row[col]
                            if val:
                                names.add(str(val))
                    except Exception:
                        continue
        except Exception:
            return []
        out: list[Entity] = []
        for name in sorted(names):
            toks = [t for t in tokens_for(name, min_len=self.min_token_len) if t not in _CORP_STOP]
            if toks:
                out.append(Entity(id=name, label=name, tokens=toks))
        return out


class PersonMatcher(EntityMatcher):
    """Match against the real paired-people roster (owner + guests, e.g. Alex, Sam, Jo). These are short
    display names, so the distinctiveness threshold drops to 3, which is safe now that matching is
    word-level ('jo' matches the word 'jo', not 'came')."""

    min_token_len = 3

    def roster(self) -> list[Entity]:
        try:
            from .auth_accounts import list_users
            users = list_users()
        except Exception:
            return []
        out: list[Entity] = []
        for u in users:
            uid = u.get("user_id") or u.get("id")
            name = u.get("display_name") or u.get("name") or ""
            if uid and name:
                out.append(Entity(id=str(uid), label=name, tokens=tokens_for(name, min_len=self.min_token_len)))
        return out
