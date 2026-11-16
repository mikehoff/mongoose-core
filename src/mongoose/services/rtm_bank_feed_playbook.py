from __future__ import annotations

import csv
import json
import uuid
from pathlib import Path
from typing import Any

from ..db import audit, connect, utc_now_iso
from ..settings import get_settings

# RTM Metro bank-feed playbook
# ----------------------------
# This module turns the research guide into an operational local playbook. It does
# NOT connect to Metro, does NOT request payment-initiation scopes, and does NOT
# move money. It helps Mongoose ask the right product/account questions, record
# provider tests, capture evidence, and export an audit pack for directors,
# leaseholders and the accountant.

DEFAULT_RECONSENT_DAYS = 90

ACCOUNT_TYPES: dict[str, dict[str, Any]] = {
    "community_current": {
        "label": "Community Current Account",
        "likely_use": "service-charge account",
        "xero": "eligible on Metro's public Xero matrix; test Xero first",
        "implication": "Test Xero direct feed first, then Crunch, then AIS/open-banking routes.",
    },
    "community_instant_access_deposit": {
        "label": "Community Instant Access Deposit Account",
        "likely_use": "reserve account",
        "xero": "ineligible on Metro's public Xero matrix",
        "implication": "Skip Xero direct feed for this account; test Crunch and AIS/open-banking routes.",
    },
    "trust_bank_account": {
        "label": "Trust Bank Account",
        "likely_use": "possible trust-fund banking product",
        "xero": "not explicitly listed on Metro's current public Xero eligibility page",
        "implication": "Get written Metro confirmation before spending time on Xero; still screen AIS routes.",
    },
    "business_bank_account": {
        "label": "Business Bank Account",
        "likely_use": "possible business-current account product",
        "xero": "supported on Metro's public Xero matrix",
        "implication": "Xero and AIS/open-banking routes are both worth testing.",
    },
    "commercial_current": {
        "label": "Commercial Current Account",
        "likely_use": "possible commercial-current account product",
        "xero": "supported on Metro's public Xero matrix",
        "implication": "Xero may require the Commercial Online Banking/postal form route; AIS routes still worth testing.",
    },
    "unknown": {
        "label": "Unknown / not yet confirmed",
        "likely_use": "must be identified before provider testing",
        "xero": "unknown",
        "implication": "Identify the exact Metro product and banking platform before testing providers.",
    },
}

PROVIDERS: dict[str, dict[str, Any]] = {
    "xero": {
        "rank": 1,
        "name": "Xero direct feed",
        "type": "accounting destination / bank-official direct feed",
        "cost_note": "Xero subscription plus Metro direct-feed charge of about £1 + VAT per account per month.",
        "evidence_signal": "Strongest official Metro signal for eligible current-account products; Community Current is eligible, Community Instant Access Deposit is not.",
        "best_for": "Service-charge account if it is Community Current, Business Bank Account or Commercial Current.",
        "expectation": "Likely pass for eligible current accounts; official fail for Community Instant Access Deposit reserve accounts.",
        "account_type_notes": {
            "community_current": "high priority test",
            "community_instant_access_deposit": "do not spend time on Xero direct feed unless Metro says otherwise in writing",
            "trust_bank_account": "written Metro confirmation first",
            "business_bank_account": "worth testing",
            "commercial_current": "worth testing; may need postal/authority form route",
        },
        "prerequisites": [
            "Xero organisation created",
            "Exact Metro product name confirmed",
            "Two authorised signatories if Metro's Authority to Disclose route is used",
            "Xero organisation name and login email",
            "Metro online-banking access",
        ],
        "steps": [
            "Create separate Xero bank accounts for service-charge and reserve accounts.",
            "Confirm the Metro product for each account before applying.",
            "If the service-charge account is an eligible Community Current/Business/Commercial current product, request the direct feed through the correct Metro/Xero path.",
            "Do not submit a Community Instant Access Deposit reserve account unless Metro has confirmed a workaround in writing.",
            "Wait for activation and capture the first feed date and refresh timestamp.",
            "Manually import historic transactions only for the period before the feed starts.",
        ],
        "verification_checks": [
            "Metro product eligibility confirmed",
            "Feed activated within expected working-day window",
            "Weekday refresh timestamps captured",
            "Service-charge and reserve ledgers remain separate",
            "No payment-initiation authority requested",
        ],
        "success_criteria": "Eligible current account imports automatically and stays refreshing for two weeks.",
        "failure_criteria": "Account product ineligible, feed rejected, organisation/email mismatch, or stale feed after activation.",
        "evidence_to_capture": ["completed form or online request", "Metro/Xero confirmation", "bank-feed-active screenshot", "first imported transaction date", "weekday refresh timestamps"],
        "risk_note": "Best bank-official route where the account product is eligible; plan another route for reserve/deposit accounts.",
    },
    "crunch": {
        "rank": 2,
        "name": "Crunch Plus",
        "type": "packaged accounting/bookkeeping destination",
        "cost_note": "Crunch Plus about £10 + VAT per month; confirm current price before subscribing.",
        "evidence_signal": "Direct Metro evidence; Metro statement uploads every Tuesday and Friday; Crunch describes the link as read-only.",
        "best_for": "Low-cost packaged test for both service-charge and reserve accounts.",
        "expectation": "Strong candidate for both accounts, but live account-type check is still required.",
        "account_type_notes": {
            "community_current": "strong test candidate",
            "community_instant_access_deposit": "test whether the reserve account appears; do not assume",
            "trust_bank_account": "ask support for product-specific confirmation",
            "business_bank_account": "strong test candidate",
            "commercial_current": "strong test candidate if shown in the feed flow",
        },
        "prerequisites": ["Crunch Plus trial/subscription", "RTM legal entity set up", "both Metro accounts added separately", "authorised Metro user available"],
        "steps": [
            "Create the RTM entity in Crunch and add each Metro account separately.",
            "Use Accounts & Cards to check whether a Metro bank feed is offered for the sort code/account number.",
            "Activate the service-charge account first, then reserve.",
            "Complete authentication if prompted and wait through at least the Tuesday/Friday cycle.",
            "Capture whether the path is direct Metro or open banking.",
        ],
        "verification_checks": ["both accounts offered", "initial history depth", "last refresh timestamp", "read-only assurance", "reconnect/expiry behaviour"],
        "success_criteria": "Both accounts link without CSV export and continue importing automatically for several days.",
        "failure_criteria": "Metro account not detected, only one account appears, stale feed beyond expected cycle, or manual import fallback only.",
        "evidence_to_capture": ["Accounts & Cards screenshots", "first transaction list", "refresh timestamp", "support reply on direct vs OB path", "bank report export"],
        "risk_note": "Useful low-cost off-the-shelf test. Still keep service-charge and reserve accounts as separate ledgers.",
    },
    "plaid": {
        "rank": 3,
        "name": "Plaid Transactions",
        "type": "developer/AISP aggregator route",
        "cost_note": "Transactions is a subscription-fee product per active Item; remove unused Items promptly.",
        "evidence_signal": "Strong developer route; Metro appears in UK coverage signals and Transactions can provide deep history and webhooks.",
        "best_for": "Thin internal dashboard or custom RTM datastore if recurring per-Item billing is acceptable.",
        "expectation": "Strong custom route if Metro appears live and both RTM accounts are exposed.",
        "account_type_notes": {
            "community_current": "screen in live Plaid Link",
            "community_instant_access_deposit": "screen whether the deposit/reserve account appears",
            "trust_bank_account": "ask Plaid/support and test live picker",
            "business_bank_account": "screen in live Plaid Link",
            "commercial_current": "screen in live Plaid Link",
        },
        "prerequisites": ["Plaid developer account", "live UK/Europe access", "webhook endpoint for freshness testing", "separate internal IDs for service-charge and reserve accounts"],
        "steps": [
            "Confirm the live Metro institution in Plaid Coverage Explorer or institutions API.",
            "Create a link token with Transactions.",
            "Run Plaid Link and authenticate with Metro.",
            "Exchange the public token and run /transactions/sync to establish the cursor.",
            "Repeat for or select both accounts as separate tracked accounts/items as appropriate.",
            "Observe webhook and refresh behaviour, then remove any unused Item.",
        ],
        "verification_checks": ["Metro institution discovery", "account picker shows both RTM accounts", "history depth", "webhook receipts", "Item removal confirmation"],
        "success_criteria": "Stable /transactions/sync data and webhook freshness for both accounts.",
        "failure_criteria": "Metro missing, only one account exposed, no transactions after consent, or pricing unacceptable.",
        "evidence_to_capture": ["institution object", "link-token log", "first sync response", "cursor values", "webhook receipt", "item/remove confirmation"],
        "risk_note": "Read-only internal-datastore candidate; watch recurring costs and consent lifecycle.",
    },
    "pandle": {
        "rank": 4,
        "name": "Pandle Pro",
        "type": "low-cost packaged accounting candidate",
        "cost_note": "Pandle Pro about £5 + VAT per month; uses Plaid as AISP agent according to public material.",
        "evidence_signal": "Worth testing because it is cheap and Plaid-backed, but public Metro proof is indirect; live bank/account-type search is decisive.",
        "best_for": "Quick low-cost packaged trial after Xero/Crunch signals are understood.",
        "expectation": "Worth testing only if the live supported-bank search confirms Metro and the exact account type.",
        "account_type_notes": {
            "community_current": "live search required",
            "community_instant_access_deposit": "live search required; do not assume reserve support",
            "trust_bank_account": "written/support confirmation advised",
            "business_bank_account": "live search required",
            "commercial_current": "live search required",
        },
        "prerequisites": ["Pandle Pro trial", "RTM entity created", "access to supported-banks search and bank-feed flow"],
        "steps": [
            "Search for Metro Bank in Pandle's supported-banks/account-type tool before linking anything.",
            "Add service-charge and reserve accounts separately.",
            "Complete the Plaid/Pandle consent flow if Metro/account type is shown.",
            "Verify automatic imports and export a sample for reconciliation.",
        ],
        "verification_checks": ["Metro shown in supported-bank search", "account type supported", "both accounts available", "refresh cadence", "export quality"],
        "success_criteria": "Both accounts connect and import automatically for several days.",
        "failure_criteria": "Metro missing, only one account type supported, or manual import fallback only.",
        "evidence_to_capture": ["supported-bank result", "feed setup screenshots", "import timestamps", "export sample", "support confirmation"],
        "risk_note": "Cheap trial candidate, not first bet for reserve unless live search confirms that product.",
    },
    "gocardless": {
        "rank": 5,
        "name": "GoCardless Bank Account Data",
        "type": "developer/AISP screening route",
        "cost_note": "Pricing is not published as a simple standalone tariff; confirm commercial terms.",
        "evidence_signal": "Strong docs and easy screening through institutions?country=gb, but no simple public Metro-specific page in the report.",
        "best_for": "Quick developer screening to see whether Metro appears in the GB institutions list.",
        "expectation": "Screening candidate pending live institutions query.",
        "account_type_notes": {
            "community_current": "screen via institutions and requisition",
            "community_instant_access_deposit": "screen whether reserve/deposit account appears after consent",
            "trust_bank_account": "ask support and verify live",
            "business_bank_account": "screen via institutions and requisition",
            "commercial_current": "screen via institutions and requisition",
        },
        "prerequisites": ["Bank Account Data account and user secrets", "HTTPS redirect URL", "storage for requisition/account IDs", "pricing confirmation"],
        "steps": [
            "Create access tokens and call institutions?country=gb.",
            "Record Metro institution ID and history/access limits if present.",
            "Create end-user agreement and requisition for accounts, balances and transactions only.",
            "Authenticate through Metro and retrieve connected account IDs.",
            "Call account details, balances and transactions for each RTM account.",
            "Observe rate limits and refresh behaviour for several days.",
        ],
        "verification_checks": ["Metro in GB institutions", "both accounts exposed", "history days", "90-day access window", "rate-limit behaviour"],
        "success_criteria": "Metro appears and both RTM accounts connect with transaction history.",
        "failure_criteria": "Metro absent, only one account appears, API rate limits impractical, or unclear pricing.",
        "evidence_to_capture": ["institutions response", "agreement object", "requisition object", "account IDs", "transactions response", "pricing support reply"],
        "risk_note": "Good read-only internal route; design for conservative scheduled refreshes.",
    },
    "moneyhub": {
        "rank": 6,
        "name": "Moneyhub",
        "type": "developer/widget screening route",
        "cost_note": "Commercial/API arrangement required; confirm current terms.",
        "evidence_signal": "Strong lifecycle docs and four-hour sync schedule, but no simple public Metro support page in the report.",
        "best_for": "Screening through bank chooser/connections widget.",
        "expectation": "Screening candidate pending live bank-chooser result.",
        "account_type_notes": {
            "community_current": "screen in bank chooser",
            "community_instant_access_deposit": "screen whether reserve/deposit account syncs",
            "trust_bank_account": "support confirmation advised",
            "business_bank_account": "screen in bank chooser",
            "commercial_current": "screen in bank chooser",
        },
        "prerequisites": ["Moneyhub API/client", "user creation", "connection/widget setup"],
        "steps": [
            "Open the Moneyhub bank chooser/connections widget.",
            "Check whether Metro appears for the live client.",
            "Authenticate and inspect resulting accounts.",
            "Observe the documented sync lifecycle and reconsent behaviour.",
        ],
        "verification_checks": ["Metro present in chooser", "both accounts returned", "sync timestamps", "expiry/reconsent data", "error codes"],
        "success_criteria": "Metro appears and both accounts stay syncing.",
        "failure_criteria": "Metro absent or reserve/current account missing after consent.",
        "evidence_to_capture": ["bank chooser screenshot", "connection ID", "first account response", "balance/transaction response", "sync timestamp"],
        "risk_note": "Viable only after live chooser confirmation.",
    },
    "yapily": {
        "rank": 7,
        "name": "Yapily",
        "type": "developer/AISP infrastructure route",
        "cost_note": "High-friction onboarding; may require Metro-side manual registration and certificates.",
        "evidence_signal": "Strong Metro-specific institution configuration with institution ID 'metro', but live use requires manual bank registration.",
        "best_for": "Bespoke integration only if you are willing to onboard properly.",
        "expectation": "Technically strong but high-friction for a small RTM.",
        "account_type_notes": {
            "community_current": "possible after manual Metro registration",
            "community_instant_access_deposit": "verify account exposure after registration",
            "trust_bank_account": "written confirmation required",
            "business_bank_account": "possible after manual Metro registration",
            "commercial_current": "possible after manual Metro registration",
        },
        "prerequisites": ["Yapily arrangement", "Metro application registration", "Open Banking certificates if required"],
        "steps": [
            "Confirm institution ID 'metro' in Yapily institutions.",
            "Complete Metro-side registration if proceeding.",
            "Create account authorisation for account information only.",
            "Redirect the authorised user and retrieve accounts/transactions.",
        ],
        "verification_checks": ["Metro registration accepted", "accounts endpoint returns both accounts", "transactions retrieved", "90-day consent state tracked"],
        "success_criteria": "Both accounts appear and transactions retrieve after proper registration.",
        "failure_criteria": "Registration burden disproportionate, account missing, or certificate/onboarding blocker.",
        "evidence_to_capture": ["institution config", "Metro registration correspondence", "authorisation ID", "account/transaction responses"],
        "risk_note": "Do not treat as a quick test; use only if a bespoke route is justified.",
    },
    "clearbooks": {
        "rank": 8,
        "name": "Clear Books Small",
        "type": "packaged accounting destination",
        "cost_note": "Small plan about £16/month before any Metro/intro discount; confirm current status and pricing.",
        "evidence_signal": "Official Metro partnership exists, but the report notes reliability caveats and partial-outage history for some Metro feed variants.",
        "best_for": "Alternative accounting package if Xero/Crunch do not fit and a one-week reliability test passes.",
        "expectation": "Possible, but lower confidence due to reliability risk.",
        "account_type_notes": {
            "community_current": "test only after checking current feed status",
            "community_instant_access_deposit": "verify reserve support explicitly",
            "trust_bank_account": "support confirmation required",
            "business_bank_account": "test current supported route",
            "commercial_current": "watch status-page caveats",
        },
        "prerequisites": ["Clear Books Small trial/subscription", "Metro app if using Metro-discount route", "separate service/reserve bank accounts in Clear Books"],
        "steps": [
            "Check Clear Books Metro feed status before starting.",
            "Create separate accounts and try the Metro app/direct route first if surfaced.",
            "If not available, test the current automatic-feed path.",
            "Observe refresh on day 1, day 3 and day 7.",
        ],
        "verification_checks": ["direct vs third-party path", "both accounts available", "status-page incidents", "staleness after day 1", "delete/re-add loops"],
        "success_criteria": "Both accounts refresh automatically for at least one week without re-adding the feed.",
        "failure_criteria": "Feed stalls, status outage, one account missing, or repeated delete/re-add required.",
        "evidence_to_capture": ["registration path screenshots", "status page screenshot", "day 1/3/7 refresh timestamps", "support replies"],
        "risk_note": "Worth testing only after stronger candidates because of reliability caveats.",
    },
    "truelayer": {
        "rank": 9,
        "name": "TrueLayer",
        "type": "developer/AISP screening route",
        "cost_note": "Console/live access required; confirm commercial terms.",
        "evidence_signal": "Strong generic data API, but public Metro support is inconclusive in the report; provider support must be checked in Console.",
        "best_for": "Screening only until Console shows Metro in live provider selection.",
        "expectation": "Unknown until Console screening.",
        "account_type_notes": {
            "community_current": "screen in Console/provider selector",
            "community_instant_access_deposit": "screen after consent if Metro present",
            "trust_bank_account": "support confirmation advised",
            "business_bank_account": "screen in Console/provider selector",
            "commercial_current": "screen in Console/provider selector",
        },
        "prerequisites": ["TrueLayer Console account", "live Data API access"],
        "steps": [
            "Inspect live Supported Providers in Console.",
            "Only build the auth link if Metro is present.",
            "Run a data-only flow and confirm both accounts appear.",
        ],
        "verification_checks": ["Metro visible in Console", "data-only scopes", "both accounts returned", "consent expiry and error handling"],
        "success_criteria": "Metro appears in live provider selection and returns both accounts.",
        "failure_criteria": "Metro absent or account types missing.",
        "evidence_to_capture": ["Console provider screenshot", "auth/consent ID", "first account response", "transaction response"],
        "risk_note": "Do not spend engineering time until live Console screening passes.",
    },
    "finverse": {
        "rank": 10,
        "name": "Finverse",
        "type": "vendor-confirmation-only candidate",
        "cost_note": "Do not test before written UK/Metro confirmation.",
        "evidence_signal": "Weakest UK option in the report; public material focuses on HK and SG, not credible UK Metro evidence.",
        "best_for": "Out of scope unless the vendor confirms UK Metro business/community account support in writing.",
        "expectation": "Do not test without written UK Metro confirmation.",
        "account_type_notes": {
            "community_current": "written confirmation first",
            "community_instant_access_deposit": "written confirmation first",
            "trust_bank_account": "written confirmation first",
            "business_bank_account": "written confirmation first",
            "commercial_current": "written confirmation first",
        },
        "prerequisites": ["Written UK Metro AIS support confirmation from Finverse"],
        "steps": [
            "Ask vendor for UK Metro Bank AIS support confirmation for business/community/trust accounts.",
            "Do not build until written confirmation and a working demo are available.",
        ],
        "verification_checks": ["written confirmation", "working sandbox/live demo", "scope limited to account information"],
        "success_criteria": "Written confirmation plus working demo.",
        "failure_criteria": "No written UK Metro confirmation.",
        "evidence_to_capture": ["vendor confirmation", "demo results", "pricing/commercial terms"],
        "risk_note": "Lowest priority and normally out of scope.",
    },
}

EVIDENCE_ITEMS = [
    ("account_identity", "Metro product page/PDF or statement showing exact account type"),
    ("authorisation", "Consent screen, completed redirect, connection ID, consent ID or signed form"),
    ("data", "First imported transaction, first transaction date, current balance, account mask/last digits"),
    ("freshness", "Last refresh time, next scheduled sync, webhook timestamp or provider status"),
    ("governance", "Read-only access or account-information-only scope"),
    ("failure", "Error message, outage status, rejection email, absent provider listing"),
    ("cost", "Provider invoice screen, pricing page screenshot or sales quote"),
    ("audit", "Export/report showing service-charge and reserve accounts stay separate"),
]

MESSAGE_TEMPLATES: dict[str, str] = {
    "metro_business": (
        "We are an RTM company holding service-charge and reserve funds.\n"
        "Please confirm, for each account ending [1234] and [5678]:\n"
        "1. the exact Metro product name;\n"
        "2. the online-banking platform attached to it;\n"
        "3. whether the account is eligible for direct feeds to Xero;\n"
        "4. whether the account is accessible through Metro's AIS / Open Banking interfaces for authorised third-party providers; and\n"
        "5. whether there are any known restrictions on Community or Trust-style accounts being connected to third-party read-only accounting/data services."
    ),
    "metro_trust": (
        "Please confirm whether our Trust Bank Account can be connected to third-party read-only account information services, "
        "and whether Metro recognises that product as eligible or ineligible for Xero direct bank feeds."
    ),
    "crunch": (
        "We are testing Metro Bank feeds for an RTM company with two trust-fund bank accounts.\n"
        "Can you confirm whether your Metro integration supports Community Current Accounts, Community Instant Access Deposit Accounts and Trust Bank Accounts, "
        "and whether the Metro path is direct Metro integration or open banking for each product type?"
    ),
    "xero_metro": (
        "We intend to connect one Metro service-charge account and one Metro reserve account. "
        "Metro's public page says Community Current is eligible and Community Instant Access Deposit is not. "
        "Please confirm whether you have any supported workaround for a reserve/deposit product, or whether a second non-Xero route is required for that account."
    ),
    "aggregators": (
        "We are a UK RTM company testing Metro Bank accounts for read-only transaction retrieval only.\n"
        "Please confirm in writing whether your live production environment currently supports: UK Metro Bank; business/community/trust-style accounts; "
        "separate current and deposit/savings accounts under the same customer; the typical historical transaction window; and whether any manual institution registration with Metro is required before live use."
    ),
    "yapily": (
        "We are a UK RTM company testing Metro Bank accounts for read-only transaction retrieval only. "
        "Yapily's Metro configuration indicates manual Metro registration may be required. "
        "Please confirm the current registration steps and whether business/community/trust-style Metro accounts are supported for account-information access."
    ),
}


def _norm_account_type(value: str | None) -> str:
    raw = (value or "unknown").strip().lower().replace("/", " ").replace("-", " ")
    raw = "_".join(raw.split())
    aliases = {
        "community_current_account": "community_current",
        "community_current": "community_current",
        "current": "community_current",
        "community_instant_access": "community_instant_access_deposit",
        "community_instant_access_deposit_account": "community_instant_access_deposit",
        "community_instant_access_deposit": "community_instant_access_deposit",
        "reserve": "community_instant_access_deposit",
        "trust": "trust_bank_account",
        "trust_account": "trust_bank_account",
        "trust_bank_account": "trust_bank_account",
        "business": "business_bank_account",
        "business_bank_account": "business_bank_account",
        "commercial": "commercial_current",
        "commercial_current_account": "commercial_current",
        "commercial_current": "commercial_current",
    }
    return aliases.get(raw, raw if raw in ACCOUNT_TYPES else "unknown")


def _norm_provider(value: str | None) -> str:
    raw = (value or "").strip().lower().replace(" ", "").replace("-", "")
    aliases = {"clearbookssmall": "clearbooks", "gocardlessbankaccountdata": "gocardless", "pandlepro": "pandle", "xerodirectfeed": "xero", "crunchplus": "crunch"}
    key = aliases.get(raw, raw)
    if key not in PROVIDERS:
        raise ValueError(f"provider must be one of {', '.join(sorted(PROVIDERS))}")
    return key


def _ensure_tables() -> None:
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS rtm_bank_feed_accounts (
                id TEXT PRIMARY KEY,
                nickname TEXT,
                purpose TEXT NOT NULL,
                metro_product_name TEXT,
                account_type TEXT NOT NULL DEFAULT 'unknown',
                online_banking_platform TEXT,
                visible_in_app INTEGER NOT NULL DEFAULT 0,
                account_mask TEXT,
                sort_code_mask TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rtm_bank_feed_accounts_purpose ON rtm_bank_feed_accounts(purpose);

            CREATE TABLE IF NOT EXISTS rtm_bank_feed_tests (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                account_id TEXT,
                status TEXT NOT NULL,
                connector_path TEXT,
                sca_required INTEGER,
                read_only_confirmed INTEGER,
                first_transaction_date TEXT,
                last_refresh_at TEXT,
                outcome TEXT,
                notes TEXT,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rtm_bank_feed_tests_provider ON rtm_bank_feed_tests(provider, status);

            CREATE TABLE IF NOT EXISTS rtm_bank_feed_evidence (
                id TEXT PRIMARY KEY,
                test_id TEXT,
                provider TEXT,
                account_id TEXT,
                evidence_type TEXT NOT NULL,
                description TEXT NOT NULL,
                location TEXT,
                captured_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rtm_bank_feed_evidence_type ON rtm_bank_feed_evidence(evidence_type, provider);
            """
        )


def account_type_matrix() -> dict[str, Any]:
    return {k: dict(v, id=k) for k, v in ACCOUNT_TYPES.items()}


def provider_ids() -> list[str]:
    return [k for k, _ in sorted(PROVIDERS.items(), key=lambda kv: int(kv[1]["rank"]))]


def provider_playbook(provider: str | None = None) -> dict[str, Any]:
    if provider:
        key = _norm_provider(provider)
        return {"provider": key, **PROVIDERS[key]}
    return {"providers": [{"id": k, **v} for k, v in sorted(PROVIDERS.items(), key=lambda kv: int(kv[1]["rank"]))]}


def governance_guardrails() -> dict[str, Any]:
    return {
        "boundary": "Read-only account information only. Mongoose does not move money, initiate payments, enable VRP, or hold bank credentials for Metro.",
        "trust_fund_rules": [
            "Service-charge and reserve accounts must stay separately identifiable.",
            "Evidence must show which bank account and ledger each import belongs to.",
            "Prefer accounts, balances and transactions scopes only.",
            "Record who can connect, reconnect and revoke each provider feed.",
        ],
        "minimum_scopes": ["accounts", "balances", "transactions", "details only if needed for reconciliation"],
        "forbidden_by_default": ["payment initiation", "variable recurring payments", "silent live bank access without director approval"],
        "reconsent_days": DEFAULT_RECONSENT_DAYS,
    }


def evidence_pack_requirements() -> list[dict[str, str]]:
    return [{"id": k, "description": v} for k, v in EVIDENCE_ITEMS]


def list_message_templates() -> dict[str, str]:
    return dict(MESSAGE_TEMPLATES)


def get_message_template(key: str) -> dict[str, str]:
    k = (key or "").strip().lower().replace("-", "_").replace(" ", "_")
    if k not in MESSAGE_TEMPLATES:
        raise ValueError(f"message key must be one of {', '.join(sorted(MESSAGE_TEMPLATES))}")
    return {"key": k, "message": MESSAGE_TEMPLATES[k]}


def upsert_account(*, id: str | None = None, nickname: str = "", purpose: str = "", metro_product_name: str = "",
                   account_type: str = "unknown", online_banking_platform: str = "", visible_in_app: bool = False,
                   account_mask: str = "", sort_code_mask: str = "", notes: str = "") -> dict[str, Any]:
    _ensure_tables()
    purpose_norm = (purpose or "").strip().lower().replace(" ", "_")
    if purpose_norm in {"service", "service_charge", "service_charge_account"}:
        purpose_norm = "service_charge"
    elif purpose_norm in {"reserve", "reserve_fund", "reserve_account"}:
        purpose_norm = "reserve"
    elif not purpose_norm:
        purpose_norm = "unknown"
    if purpose_norm not in {"service_charge", "reserve", "operating", "unknown"}:
        purpose_norm = "unknown"
    acct_type = _norm_account_type(account_type or metro_product_name)
    acct_id = (id or "").strip() or "acct_" + uuid.uuid4().hex[:12]
    now = utc_now_iso()
    with connect() as con:
        existing = con.execute("SELECT created_at FROM rtm_bank_feed_accounts WHERE id=?", (acct_id,)).fetchone()
        created_at = existing["created_at"] if existing else now
        con.execute(
            """
            INSERT INTO rtm_bank_feed_accounts(id,nickname,purpose,metro_product_name,account_type,online_banking_platform,
                visible_in_app,account_mask,sort_code_mask,notes,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                nickname=excluded.nickname, purpose=excluded.purpose, metro_product_name=excluded.metro_product_name,
                account_type=excluded.account_type, online_banking_platform=excluded.online_banking_platform,
                visible_in_app=excluded.visible_in_app, account_mask=excluded.account_mask,
                sort_code_mask=excluded.sort_code_mask, notes=excluded.notes, updated_at=excluded.updated_at
            """,
            (acct_id, nickname, purpose_norm, metro_product_name, acct_type, online_banking_platform,
             1 if visible_in_app else 0, account_mask, sort_code_mask, notes, created_at, now),
        )
    audit("rtm_bank_feed", "upsert_account", True, payload={"purpose": purpose_norm, "account_type": acct_type})
    return {"ok": True, "id": acct_id, "purpose": purpose_norm, "account_type": acct_type, "guidance": ACCOUNT_TYPES[acct_type]["implication"]}


def list_accounts() -> list[dict[str, Any]]:
    _ensure_tables()
    with connect() as con:
        rows = con.execute("SELECT * FROM rtm_bank_feed_accounts ORDER BY purpose, created_at").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["visible_in_app"] = bool(d.get("visible_in_app"))
        d["account_type_label"] = ACCOUNT_TYPES.get(d.get("account_type"), ACCOUNT_TYPES["unknown"])["label"]
        d["implication"] = ACCOUNT_TYPES.get(d.get("account_type"), ACCOUNT_TYPES["unknown"])["implication"]
        out.append(d)
    return out


def record_test(*, provider: str, account_id: str = "", status: str = "planned", connector_path: str = "",
                sca_required: bool | None = None, read_only_confirmed: bool | None = None,
                first_transaction_date: str = "", last_refresh_at: str = "", outcome: str = "", notes: str = "",
                id: str | None = None) -> dict[str, Any]:
    _ensure_tables()
    prov = _norm_provider(provider)
    stat = (status or "planned").strip().lower().replace(" ", "_")
    if stat not in {"planned", "screening", "in_progress", "passed", "failed", "blocked", "abandoned", "watching"}:
        stat = "planned"
    test_id = (id or "").strip() or "test_" + uuid.uuid4().hex[:12]
    now = utc_now_iso()
    sca = None if sca_required is None else (1 if sca_required else 0)
    ro = None if read_only_confirmed is None else (1 if read_only_confirmed else 0)
    with connect() as con:
        existing = con.execute("SELECT started_at FROM rtm_bank_feed_tests WHERE id=?", (test_id,)).fetchone()
        started_at = existing["started_at"] if existing else now
        con.execute(
            """
            INSERT INTO rtm_bank_feed_tests(id,provider,account_id,status,connector_path,sca_required,read_only_confirmed,
                first_transaction_date,last_refresh_at,outcome,notes,started_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                provider=excluded.provider, account_id=excluded.account_id, status=excluded.status,
                connector_path=excluded.connector_path, sca_required=excluded.sca_required,
                read_only_confirmed=excluded.read_only_confirmed, first_transaction_date=excluded.first_transaction_date,
                last_refresh_at=excluded.last_refresh_at, outcome=excluded.outcome, notes=excluded.notes, updated_at=excluded.updated_at
            """,
            (test_id, prov, account_id, stat, connector_path, sca, ro, first_transaction_date, last_refresh_at, outcome, notes, started_at, now),
        )
    audit("rtm_bank_feed", "record_test", True, payload={"provider": prov, "status": stat})
    return {"ok": True, "id": test_id, "provider": prov, "status": stat}


def list_tests(provider: str | None = None, account_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    _ensure_tables()
    where = []
    params: list[Any] = []
    if provider:
        where.append("provider=?")
        params.append(_norm_provider(provider))
    if account_id:
        where.append("account_id=?")
        params.append(account_id)
    if status:
        where.append("status=?")
        params.append(status)
    clause = "WHERE " + " AND ".join(where) if where else ""
    with connect() as con:
        rows = con.execute(f"SELECT * FROM rtm_bank_feed_tests {clause} ORDER BY updated_at DESC", params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["provider_name"] = PROVIDERS.get(d.get("provider"), {}).get("name", d.get("provider"))
        if d.get("sca_required") is not None:
            d["sca_required"] = bool(d["sca_required"])
        if d.get("read_only_confirmed") is not None:
            d["read_only_confirmed"] = bool(d["read_only_confirmed"])
        out.append(d)
    return out


def record_evidence(*, evidence_type: str, description: str, provider: str = "", account_id: str = "",
                    test_id: str = "", location: str = "", captured_at: str = "") -> dict[str, Any]:
    _ensure_tables()
    ev_type = (evidence_type or "").strip().lower().replace(" ", "_")
    allowed = {k for k, _ in EVIDENCE_ITEMS}
    if ev_type not in allowed:
        ev_type = "audit"
    ev_id = "ev_" + uuid.uuid4().hex[:12]
    prov = _norm_provider(provider) if provider else ""
    now = utc_now_iso()
    with connect() as con:
        con.execute(
            """
            INSERT INTO rtm_bank_feed_evidence(id,test_id,provider,account_id,evidence_type,description,location,captured_at,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (ev_id, test_id, prov, account_id, ev_type, description, location, captured_at or now, now),
        )
    audit("rtm_bank_feed", "record_evidence", True, payload={"provider": prov, "evidence_type": ev_type})
    return {"ok": True, "id": ev_id, "evidence_type": ev_type}


def list_evidence(provider: str | None = None, account_id: str | None = None, test_id: str | None = None) -> list[dict[str, Any]]:
    _ensure_tables()
    where = []
    params: list[Any] = []
    if provider:
        where.append("provider=?")
        params.append(_norm_provider(provider))
    if account_id:
        where.append("account_id=?")
        params.append(account_id)
    if test_id:
        where.append("test_id=?")
        params.append(test_id)
    clause = "WHERE " + " AND ".join(where) if where else ""
    with connect() as con:
        rows = con.execute(f"SELECT * FROM rtm_bank_feed_evidence {clause} ORDER BY captured_at DESC", params).fetchall()
    return [dict(r) for r in rows]


def _has_test(provider: str, account_id: str | None = None) -> bool:
    tests = list_tests(provider=provider, account_id=account_id or None)
    return bool(tests)


def _acct_by_purpose(accounts: list[dict[str, Any]], purpose: str) -> list[dict[str, Any]]:
    return [a for a in accounts if a.get("purpose") == purpose]


def next_actions() -> list[dict[str, Any]]:
    accounts = list_accounts()
    tests = list_tests()
    actions: list[dict[str, Any]] = []
    if not accounts:
        actions.append({
            "urgency": "high",
            "title": "Create Metro account inventory",
            "action": "Record product name, banking platform, purpose, app visibility and account type for the service-charge and reserve accounts before testing providers.",
        })
        return actions
    for a in accounts:
        if a.get("account_type") == "unknown" or not a.get("online_banking_platform"):
            actions.append({
                "urgency": "high",
                "title": f"Complete inventory for {a.get('nickname') or a.get('purpose') or a.get('id')}",
                "action": "Exact Metro product and online-banking platform are still missing; provider testing would be ambiguous.",
            })
    service_accounts = _acct_by_purpose(accounts, "service_charge")
    reserve_accounts = _acct_by_purpose(accounts, "reserve")
    if service_accounts:
        svc = service_accounts[0]
        if svc.get("account_type") in {"community_current", "business_bank_account", "commercial_current"} and not _has_test("xero", svc.get("id")):
            actions.append({"urgency": "high", "title": "Test Xero for service-charge account", "action": "Account type looks eligible; run the Xero direct-feed test and capture activation/freshness evidence."})
    if reserve_accounts:
        res = reserve_accounts[0]
        if res.get("account_type") == "community_instant_access_deposit" and not any(_has_test(p, res.get("id")) for p in ("crunch", "plaid", "pandle", "gocardless", "moneyhub")):
            actions.append({"urgency": "high", "title": "Do not waste time on Xero for reserve", "action": "Community Instant Access Deposit is expected to fail Xero direct feed; test Crunch and one AIS screening route instead."})
    if not any(t.get("provider") == "crunch" for t in tests):
        actions.append({"urgency": "medium", "title": "Run Crunch Plus test", "action": "Crunch is the strongest low-cost packaged candidate for both accounts; capture whether the link is direct Metro or open banking."})
    if not any(t.get("provider") in {"plaid", "gocardless"} for t in tests):
        actions.append({"urgency": "medium", "title": "Run one developer screening", "action": "Use Plaid coverage/Link or GoCardless institutions?country=gb to verify whether Metro and both account types appear live."})
    if tests and not any(e.get("evidence_type") == "governance" for e in list_evidence()):
        actions.append({"urgency": "medium", "title": "Capture read-only governance proof", "action": "Every provider test should include evidence that access is account-information/read-only only."})
    actions.append({"urgency": "low", "title": "Keep trust funds separate", "action": "Maintain separate destination ledgers/accounts for service-charge and reserve funds throughout every test."})
    return actions


def playbook_status() -> dict[str, Any]:
    chosen = (getattr(get_settings(), "metro_ob_provider", "") or "").strip().lower()
    if chosen:
        try:
            chosen = _norm_provider(chosen)
        except ValueError:
            pass
    return {
        "title": "RTM Metro bank-feed verification playbook",
        "accounts": list_accounts(),
        "tests": list_tests(),
        "evidence": list_evidence(),
        "providers": [{"id": k, "name": v["name"], "rank": v["rank"], "expectation": v["expectation"], "best_for": v["best_for"]} for k, v in sorted(PROVIDERS.items(), key=lambda kv: int(kv[1]["rank"]))],
        "account_type_matrix": account_type_matrix(),
        "next_actions": next_actions(),
        "evidence_requirements": evidence_pack_requirements(),
        "governance": governance_guardrails(),
        "chosen_provider": chosen or None,
        "boundary": governance_guardrails()["boundary"],
    }


def format_status() -> str:
    s = playbook_status()
    lines = ["RTM Metro bank-feed playbook", ""]
    if not s["accounts"]:
        lines.append("No Metro account inventory recorded yet. First step: identify the exact product/platform for service-charge and reserve accounts.")
    else:
        lines.append("Accounts:")
        for a in s["accounts"]:
            lines.append(f"- {a.get('purpose')}: {a.get('account_type_label')} · {a.get('online_banking_platform') or 'platform missing'} · {a.get('implication')}")
    lines.append("")
    lines.append("Recommended provider order: " + ", ".join(f"{p['rank']}. {p['name']}" for p in s["providers"]))
    lines.append("")
    lines.append("Next actions:")
    for a in s["next_actions"][:6]:
        lines.append(f"- [{a['urgency']}] {a['title']}: {a['action']}")
    lines.append("")
    lines.append(s["boundary"])
    return "\n".join(lines)


def export_pack(path: str | Path) -> dict[str, Any]:
    _ensure_tables()
    root = Path(path)
    root.mkdir(parents=True, exist_ok=True)
    status = playbook_status()
    files: list[Path] = []
    (root / "provider_setup").mkdir(exist_ok=True)
    (root / "test_run").mkdir(exist_ok=True)
    (root / "metro_product_proof").mkdir(exist_ok=True)
    (root / "support_correspondence").mkdir(exist_ok=True)

    json_path = root / "rtm-bank-feed-playbook.json"
    json_path.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")
    files.append(json_path)

    md = ["# RTM Metro bank-feed verification playbook", "", format_status(), "", "## Governance guardrails"]
    for item in governance_guardrails()["trust_fund_rules"]:
        md.append(f"- {item}")
    md.append("")
    md.append("## Provider playbooks")
    for p in provider_ids():
        prov = PROVIDERS[p]
        md.append(f"### {prov['rank']}. {prov['name']}")
        md.append(prov["expectation"])
        md.append("")
        md.append("Steps:")
        for i, step in enumerate(prov["steps"], 1):
            md.append(f"{i}. {step}")
        md.append("")
    md_path = root / "rtm-bank-feed-playbook.md"
    md_path.write_text("\n".join(md), encoding="utf-8")
    files.append(md_path)

    msg_path = root / "support-message-templates.md"
    msg_lines = ["# Support message templates", ""]
    for k, v in MESSAGE_TEMPLATES.items():
        msg_lines.extend([f"## {k}", "", v, ""])
    msg_path.write_text("\n".join(msg_lines), encoding="utf-8")
    files.append(msg_path)

    for name, rows in {
        "account-inventory.csv": list_accounts(),
        "provider-test-register.csv": list_tests(),
        "evidence-register.csv": list_evidence(),
    }.items():
        p = root / name
        keys = sorted({k for row in rows for k in row.keys()}) or ["empty"]
        with p.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            for row in rows:
                w.writerow({k: row.get(k, "") for k in keys})
        files.append(p)
    audit("rtm_bank_feed", "export_pack", True, payload={"dir": str(root), "files": len(files)})
    return {"ok": True, "dir": str(root), "file_count": len(files), "files": [str(p) for p in files]}