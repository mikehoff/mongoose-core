# RTM Document Intake

Governed feature note (per `docs/RTM_SPEC_GOVERNANCE.md`: a requirement is real only when it has an
acceptance test; this note plus the tests are how a future session recovers intent). Owner: the
director (M). Added 2026-07-08.

## What it is

Owner and (later) RTM members upload any file (receipt, invoice, contract, ticket, feedback,
complaint, photo evidence, other evidence). Each file is stored locally, text-extracted (Apple Vision
OCR on-device for images and scanned PDFs), indexed into the unified `files`/`file_fts` search, linked
into the governed RTM evidence store, and correlated with the live Example Court records plus the
voicemails and emails Mongoose already sees.

This is RTM *operations* (evidence intake), not RTM *product build*. See
`docs/RTM_BUILD_SOURCE_OF_TRUTH.md` for the two-layer split.

## How it relates to what already existed (reconciliation)

This feature was first built standalone and then reconciled against the governed RTM system:

- **Evidence.** Every ingested document with a canonical flat is registered in `rtm_mgmt_evidence` via
  `rtm_management.add_evidence(target_type="flat", target_id=<flat>, document_kind="intake_<type>",
  ...)`, so it appears in referral packs, the management dashboard and evidence exports. The richer
  intake state (OCR text, document type, correlation links) lives in `rtm_documents` /
  `rtm_doc_links` because the evidence table does not hold those.
- **Canonical flats.** Flat references are normalised to the canonical ids `1ECF1..3ECF9`
  (`rtm_docs.canonical_flat`). Correlation matches against real `rtm_mgmt_flats` (by payment
  reference / surname) and `rtm_mgmt_demands` (by document number like `26-TO-DEC/1ECF1R0`), not just
  fuzzy full text.
- **Security.** Mongoose is the source of truth; WordPress is transit only. Documents that reach
  Mongoose from the WordPress plugin are NaCl sealed-box encrypted in transit (reusing the portal
  claim keypair, `decrypt_claim_payload`). Raw documents, raw payment references, and raw document
  numbers must never rest in WordPress in the clear (RTM_PORTAL_SPEC.md security rules).
- **Roles.** Uploaders: director, admin, leaseholder. Auditor is read-only and may only attach notes
  (`add_note`), consistent with the auditor checklist's read-only / gated-write posture. No new
  immutable auditor application role is introduced.

## Surfaces

- Cockpit (owner): upload / list / detail / reprocess / note / pull, under `/cockpit/rtm/documents*`
  and `/cockpit/rtm/pull`.
- Plugin (later): `~/mongoose-plugin/RTM_DOCUMENT_INTAKE_SPEC.md` is the in-tree build spec for the
  WordPress side; Mongoose pulls from `example-court/v1/documents/*`.

Note on building the plugin: per governance, RTM feature-building is driven by a brief and made real
by acceptance tests; the WordPress plugin half is built through the plugin author lane
(`plugin_author.py`, confined to the plugin dir) from that in-tree spec. It is not a generic chat action.

## Relationship to the leaseholder portal brief (reconciliation)

Checked against the authoritative layered brief (the RTM leaseholder WordPress portal brief + the
trust-reconciliation build log). Findings:

- This document intake is ADDITIVE to that brief. The brief's leaseholder-to-RTM submission path is
  the ISSUE system (`intake_portal_issue`, kind `portal_issue`) with optional issue attachments
  (brief section 12), not a standalone document-upload feature with document types. This feature was
  requested separately by the director and is governed by this note plus its tests.
- The owner's LOCAL intake accepts any file (including zip): it is stored on the Mac and never
  web-served, so nothing can execute. The brief section 12.4 executable/markup allowlist and 10MB /
  5-file cap apply to the LEASEHOLDER web upload path in the plugin, not the local store.
- Encryption is aligned with brief section 6.1 (asymmetric keypair, public key to WordPress,
  sodium_crypto_box_seal, Mongoose pulls + decrypts locally).
- Snapshot exclusions (brief section 8) and minimisation (section 7) constrain what Mongoose PUBLISHES
  to WordPress. Document intake is the reverse direction (WordPress to Mongoose) and the owner-facing
  view is local, so those exclusions are not bypassed.
- The trust-reconciliation build (`rtm_trust.py`, plan `example-court-rtm-trust-reconciliation`)
  exists as the build log describes; its receipt evidence kind is `service_charge_receipt`, distinct
  from this feature's `intake_<type>`, so they do not collide.

Open discrepancies to resolve with the director (not code bugs):
- The brief names a dedicated plugin `wordpress-plugin/example-court-leaseholder-portal.php`; the live
  plugin is `~/mongoose-plugin/example-court-rtm.php` and the named portal plugin does not exist. The
  document-intake in-tree spec currently targets the existing plugin.
- The build log versions the package as 1.1.3; live Mongoose is 1.53.x. The log predates the current
  version line.

## Source of truth (tests)

- `tests/test_rtm_docs.py` — ingest, doc types, roles/gating, auditor notes, OCR wiring, canonical
  flat linkage + evidence registration.
- `tests/test_rtm_pull.py` — the CGNAT-safe pull over the signed portal channel, sealed-box decrypt,
  role mapping, ack / poison-loop safety.

If code and this doc ever disagree, the tests win and this doc is wrong.
