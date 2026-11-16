from __future__ import annotations

from typing import Any

from ..settings import get_settings
from .rtm_bank_feed_playbook import (
    DEFAULT_RECONSENT_DAYS,
    PROVIDERS as PLAYBOOK_PROVIDERS,
    governance_guardrails,
    provider_ids,
    playbook_status,
)

# Backwards-compatible Metro/Open-Banking status facade.
#
# The actual implementation now lives in rtm_bank_feed_playbook.py. That service
# reflects the current RTM research: Xero direct feed first for eligible current
# accounts, Crunch Plus second as the strongest packaged candidate, and Plaid /
# Pandle / GoCardless / Moneyhub / Yapily / Clear Books / TrueLayer / Finverse as
# ranked tests or screening candidates. Mongoose still does NOT connect to Metro
# directly and does NOT move money.


def _compat_provider(provider_id: str, p: dict[str, Any]) -> dict[str, str]:
    return {
        "name": str(p["name"]),
        "kind": str(p["type"]),
        "metro_support": str(p["evidence_signal"]),
        "reconsent": f"Open-banking/AIS routes typically need reconsent around {DEFAULT_RECONSENT_DAYS} days; direct-feed paths follow the provider's own authority/revocation process.",
        "best_for": str(p["best_for"]),
        "cost": str(p["cost_note"]),
        "note": str(p["risk_note"]),
        "expectation": str(p["expectation"]),
        "rank": str(p["rank"]),
    }


RECOGNISED_PROVIDERS: dict[str, dict[str, str]] = {
    k: _compat_provider(k, v) for k, v in PLAYBOOK_PROVIDERS.items()
}


def reconsent_notice() -> str:
    return (
        "Open-banking account-information access is not permanent. Many AIS paths use a consent "
        f"window of about {DEFAULT_RECONSENT_DAYS} days, after which the authorised user must "
        "reconfirm through the provider or bank flow. Direct Metro/Xero-style feeds have their own "
        "authority and revocation process. If consent lapses, the feed stops until reconnected. "
        "The local Metro CSV import remains available as a fallback."
    )


def legal_structure_note() -> str:
    return (
        "The RTM company is a private company LIMITED BY GUARANTEE, not a sole trader, and service-charge/reserve money "
        "must be treated as trust-fund money. Do not rely on sole-trader-only offers, do not mix "
        "service-charge and reserve ledgers, and keep provider access read-only/account-information-only."
    )


def list_providers() -> dict[str, Any]:
    status = playbook_status()
    return {
        "default_path": "local Metro CSV import/folder-watch, no cloud party, no bank credentials in Mongoose",
        "alternative_path": "provider-managed direct feed or AIS/open-banking route, always read-only and explicitly tested",
        "legal_structure": legal_structure_note(),
        "recommended_order": provider_ids(),
        "cheapest_strong_packaged_candidate": "crunch (Crunch Plus, about £10+VAT/month, direct Metro evidence)",
        "cheapest_low_cost_trial_candidate": "pandle (Pandle Pro, about £5+VAT/month, but only after live Metro/account-type search confirms support)",
        "providers": RECOGNISED_PROVIDERS,
        "playbook": status,
        "reconsent": reconsent_notice(),
        "boundary": governance_guardrails()["boundary"],
    }


def selected_provider() -> str | None:
    raw = (getattr(get_settings(), "metro_ob_provider", "") or "").strip().lower()
    return raw if raw in RECOGNISED_PROVIDERS else None


def status() -> dict[str, Any]:
    prov_key = selected_provider()
    base = playbook_status()
    if prov_key is None:
        return {
            "ob_enabled": False,
            "active_path": "local CSV import / provider playbook not yet nominated",
            "providers_available": provider_ids(),
            "playbook": base,
            "note": "No Metro provider nominated in MONGOOSE_METRO_OB_PROVIDER. Use the playbook to inventory accounts and test providers before choosing one.",
            "boundary": governance_guardrails()["boundary"],
        }
    prov = RECOGNISED_PROVIDERS[prov_key]
    return {
        "ob_enabled": True,
        "active_path": f"Provider-managed read-only feed via {prov['name']}",
        "provider": prov_key,
        "provider_detail": prov,
        "reconsent_days": DEFAULT_RECONSENT_DAYS,
        "reconsent": reconsent_notice(),
        "where_consent_lives": f"Consent and the live feed are managed in {prov['name']}, not in Mongoose.",
        "fallback": "The local Metro CSV import/folder-watch path remains available if the feed lapses.",
        "playbook": base,
        "boundary": governance_guardrails()["boundary"],
    }
