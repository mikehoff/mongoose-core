# RTM Leaseholder Portal — Implementation Spec

**Version:** 0.1.0-skeleton  **Status:** Skeleton implemented, production pending

## Architecture

Snapshot-push. Mongoose is the ONLY source of truth. WordPress is display surface only.

- Mongoose → builds minimised snapshot → pushes to WordPress
- Claims → encrypted in WordPress → pulled and validated by Mongoose → decision pushed back
- Leaseholder browsers NEVER call Mongoose directly

## Files

| File | Purpose |
|------|---------|
| `src/mongoose/services/rtm_portal.py` | All Mongoose portal logic |
| `wordpress-plugin/example-court-leaseholder-portal.php` | WordPress plugin |
| `docs/RTM_PORTAL_SPEC.md` | This document |

## Flats

18 flats: 1 Example Court Flats 1–9, 3 Example Court Flats 1–9.
Flat IDs: `1ECF1`–`1ECF9`, `3ECF1`–`3ECF9`.
Data source: `src/mongoose/services/rtm_example_court.py` (EXAMPLE_COURT_FLATS).

## What's implemented (skeleton)

- `portal_readiness()` — checks bank details, WP config, keypair, PDF dir, flat count
- `build_portal_snapshot()` — assembles minimised snapshot with exclusion regression gate
- `publish_portal_snapshot()` — stub: POST to WP sync endpoint
- `pull_pending_claims()` — stub: GET from WP
- `process_pending_claims()` — stub: decrypt + validate + push decisions
- `_validate_claim()` — stub: always returns director_review until implemented
- `intake_portal_issue()` — wires to rtm_intake.intake_email with kind=portal_issue
- `attach_demand_pdf()` — computes sha256, generates pdf_key, stub storage
- Endpoints: GET/POST /rtm/portal/{readiness,snapshot,publish,pull-claims,process-claims,pull-issues,attach-demand-pdf,generate-keypair,public-key}
- CLI: `mongoose rtm portal {readiness,snapshot,publish,pull-claims,process-claims,pull-issues,generate-keypair,public-key,seed-flats,attach-demand-pdf}`
- Scheduler: `portal_process_claims` (every 15m), `portal_pull_issues` (every 60m)
- Brain tool: `rtm_portal` (READ_TOOLS: summary/readiness/snapshot)
- WordPress plugin: all 6 tables, role, shortcodes, REST endpoints, admin screens (stubs)

## What needs implementing (DELTA)

### Priority 1 — makes the portal usable

1. **PyNaCl keypair**: `pip install PyNaCl`, implement `generate_claim_keypair()` and `get_claim_public_key()`
2. **Claim validation**: implement `_validate_claim()` — look up payment_reference in rtm_mgmt_flats, check document_number against rtm_mgmt_demands, compare email
3. **WordPress claim encryption**: PHP `sodium_crypto_box_seal()` in `pc_portal_rest_claim_start()`
4. **Snapshot push**: implement `publish_portal_snapshot()` — POST to WP with HMAC signature
5. **Snapshot receive**: implement `pc_portal_rest_receive_snapshot()` — upsert flats + demands in WP tables
6. **Claim pull + decision push**: implement `pull_pending_claims()` and `push_claim_decisions()`
7. **WordPress claim UI**: implement registration form, email verification, dashboard

### Priority 2 — completes the portal

8. **PDF handling**: implement `attach_demand_pdf()` storage, PHP download endpoint with sha256 verify
9. **Bank details display**: render from WordPress options (bank_name, sort_code, account_number) with copy buttons
10. **Account statement**: render from `statement_json` in wp_pc_portal_flats with 7-day caveat
11. **Issue form**: implement `pc_portal_issue_form()` with all categories and payment issue fields
12. **Issue sync**: implement `pull_portal_issues()` to call `intake_portal_issue()`
13. **Director review screen**: implement `pc_portal_admin_claims()` with all spec §5.4 fields

### Security requirements (never skip)

- Rate limiting: IP + global + per-email + per-flat (spec §5.7)
- NEVER log raw payment_reference or document_number (spec §5.8)
- NEVER store raw refs in WordPress persistent storage (spec §5.1, §15.3)
- NEVER reveal which claim field failed (always use CLAIM_FAILURE_MESSAGE)
- NEVER expose pdf_storage_path to browser or API (spec §9.3)
- `_assert_no_excluded_fields()` must run on every snapshot before push

## Non-goals (enforced)

The portal does not take payments, confirm payments, mark payments as received,
create ledger entries, or alter bank reconciliation data.

No FTT strategy, witness statements, skeleton arguments, without-prejudice material,
director-only notes, supplier payables, contractor quotes, internal evidence,
legal advice, Mongoose audit logs, raw bank transactions, or other leaseholders'
personal data may ever appear in the snapshot.
