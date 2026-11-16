# RTM data model: the canonical stores (anti-drift)

Governed note (per `docs/RTM_SPEC_GOVERNANCE.md`). This exists because "RTM" grew three things called
"receipt" and four places a document can live, and nothing said which is canonical , so features
re-forked them. This pins the model. When something disagrees, the committed tests win; keep this in
step. Added 2026-07-09.

The live data is currently near-empty (rtm_trust_receipts 0 rows, rtm_documents 1, rtm_mgmt_evidence 0,
rtm_receipts table not yet created), so this is a NAMING/ownership consolidation, not a data migration.

## The three "receipt" concepts , distinct, do not merge

| Concept | Table / owner | What it is |
| --- | --- | --- |
| **Payment receipt** (CANONICAL "receipt") | `rtm_trust_receipts` , `rtm_trust.py` | A leaseholder's service-charge PAYMENT receipt: `RCPT-26-<flat>-<docnum>`, branded PDF, issued only when a demand's outstanding is zero. THIS is what "an RTM receipt" means. |
| **Intake action log** | `rtm_case_actions` , `rtm_intake.py` | NOT receipts: an audit log of case/draft actions (`draft_created`, `sent`, `cancelled`...). Renamed from the misleading `rtm_receipts` on 2026-07-09 (was empty/lazy, no migration); helper `case_actions()`/`_log_case_action()`, id prefix `act_`. Guarded by tests/test_rtm_intake.py. |
| **Uploaded document of type receipt** | `rtm_documents.doc_type='receipt'` , `rtm_docs.py` | A FILE the owner/leaseholder uploaded that happens to be a receipt. It is a DOCUMENT, not a receipt record. `doc_type` is just the document's nature. |

Rule: a payment receipt is `rtm_trust_receipts`. Do not add a fourth "receipt" table; do not conflate an
uploaded receipt file with a payment receipt record.

## The one evidence home + its feeders/views

| Store | Role |
| --- | --- |
| **`rtm_mgmt_evidence`** (`rtm_management.add_evidence`) | CANONICAL evidence: the governed record that a document exists and relates to a flat/demand (target_type/target_id, document_kind, sha256, path). Surfaces in referral packs, the management dashboard, evidence exports. |
| `rtm_documents` (`rtm_docs.py`) | Intake + OCR + correlation layer. Every ingested doc with a canonical flat REGISTERS into `rtm_mgmt_evidence` (done). Holds the richer state (OCR text, doc_type, correlation links) the evidence table does not. |
| `assets` (`assets.py`, `rtm_candidates`) | Upload/enrichment CANDIDATES. rtm_intake reads them read-only for enrichment. GAP: assets do not yet register into `rtm_mgmt_evidence` , a future feeder to wire. |
| `files` / `file_fts` | The unified SEARCH INDEX (a VIEW). rtm_documents index into it so uploads are globally searchable. Not a source of truth. |

Rule: the source of truth for "we hold this document/evidence" is `rtm_mgmt_evidence`. Everything else
either feeds it (rtm_documents, assets) or is a search view (files/file_fts).

## Canonical flats

`1ECF1`..`1ECF9`, `3ECF1`..`3ECF9` (`rtm_example_court.EXAMPLE_COURT_FLATS`). Payment reference =
surname uppercased. Demand document number scheme `{YY}-TO-{MON}/{flat}R{rev}` (e.g. `26-TO-DEC/1ECF1R0`).
`rtm_docs.canonical_flat()` normalises to these; correlation matches real flats/demands, not free text.

## Open decision (director's call)

The WordPress plugin: the portal brief names `wordpress-plugin/example-court-leaseholder-portal.php`
which does not exist; the live plugin is `~/mongoose-plugin/example-court-rtm.php`. Pick ONE canonical
plugin and record it here before building the leaseholder-facing document/portal surface.

## Done

- Renamed `rtm_receipts` -> `rtm_case_actions` in `rtm_intake.py` (2026-07-09, task #27). Empty/lazy
  table so no migration; killed the "receipt" ambiguity, with tests/test_rtm_intake.py guarding it.

## Deferred (safe, no data at stake yet)

- Wire `assets` to register into `rtm_mgmt_evidence` so uploaded candidates become governed evidence.
