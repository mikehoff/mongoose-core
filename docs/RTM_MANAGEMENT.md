# Example Court RTM management spine

This layer implements the uploaded Example Court RTM management-system requirements as a local, single-operator operating register. It is deliberately not a live banking or sending system.

## Operations vs product build

This document is about **RTM operations**: the service-charge management spine and its records. It is not the canonical source for the software/product build that advances Mongoose/WordPress/admin capability. For that split, and for the rules about chat/UI/CLI/scheduler/Claude source-of-truth, see `docs/RTM_BUILD_SOURCE_OF_TRUTH.md`.

## What it covers

- **R1 Demand register**: immutable demand rows, due date defaults, status, revision/supersedes links.
- **R2 Metro Bank reconciliation**: CSV import, exact reference+amount auto-allocation, partial/unmatched/duplicate review queue, manual split allocation.
- **R3 Arrears**: overdue refresh, day+1/+14/+28/+56/+90 escalation, draft reminder/recovery text, release log, referral-pack scaffold.
- **R4 Service-charge ledger**: per-flat running balance from demands, bank-backed allocations and credits; corrections should be reversing entries, not edits.
- **R5 Payables**: supplier payable schedule, approval/paid state, recurring cadence notes, cashflow over 1/3/6 months.
- **R6 Budget vs actuals**: budget line, spent, committed and variance.
- **R7 Evidence**: evidence metadata and SHA-256 for local files, attached to demands, budget lines, payables, suppliers, flats or reminders.

## Safety boundaries

Mongoose does **not** send demands, reminders or debt letters automatically. It does **not** connect to Metro Bank/Open Banking, file with authorities, connect to a live WordPress production instance, or move money. It prepares inspectable records, review queues and artefacts for the director/operator.

## Useful commands

```bash
mongoose rtm manage seed-flats
mongoose rtm manage flat 1EC 1 --name "Leaseholder" --payment-reference SURNAME
mongoose rtm manage demand 26-TO-DEC/1ECF1R0 1ECF1 "Half-year ending 31 Dec 2026" 901.23 --issue-date 2026-07-16 --delivery-method Docmail
mongoose rtm manage metro-import metro.csv
mongoose rtm manage review-payments
mongoose rtm manage allocate BANK_ID 26-TO-DEC/1ECF1R0:901.23
mongoose rtm manage arrears
mongoose rtm manage reminder 26-TO-DEC/1ECF1R0
mongoose rtm manage statement 1ECF1
mongoose rtm manage payable "Cleaner Ltd" "Monthly cleaning" sch1 120.00 --budget-line cleaning
mongoose rtm manage budget 2026 sch1 cleaning 2400.00
mongoose rtm manage budget-actuals --year 2026
mongoose rtm manage evidence demand 26-TO-DEC/1ECF1R0 docmail-proof "Docmail proof" --path proof.pdf
mongoose build-plan-example-court-management
```
