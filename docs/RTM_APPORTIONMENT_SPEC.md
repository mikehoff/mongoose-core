# RTM Two-Schedule Apportionment, Reconciliation and Credit Notes

Formal specification, version 1.0 (draft for build). Target: Mongoose `rtm_management` / `rtm_bookkeeper`
plus the Example Court WordPress plugin. Author: Claude Code, for A Morgan. Codename: sillygoose.

Money is stored as integer **pence** throughout. All dates ISO. All identifiers opaque strings.

> **Amendment v1.1 (2026-07-11), driven by the real deed data.** Shares are stored as integer **parts per
> million (ppm)**, not permille: real lease percentages carry four decimals (6.1667% = 61667 ppm) that
> permille cannot hold. Real deed percentages also do not sum to exactly 100% (Example Court Schedule 1
> = 100.0002%, undercroft = 100.008%, the developer's own rounding), so the ppm are treated as **weights**:
> the allocator apportions each schedule's total in exact proportion, penny-exact, regardless of drift.
> Validity (R10/R18) is a **tolerance** check (±0.5% ≈ ±5000 ppm): within it the drift is deed rounding and
> billing proceeds; beyond it a flat is likely missing and billing is refused. Wherever this document says
> "permille" / "sum to exactly 1000", read "ppm" / "within tolerance of 1,000,000". Verified live: all 18
> Example Court flats bill penny-exact to £15,160 (Sch1) + £2,525 (Sch2) = £17,685.

---

## 1. Background and problem statement

The Example Court budget (year to 30 June 2027) is structured as **two schedules**:

- **Schedule 1, Building service costs** (20 lines, subtotal £30,320), which includes the reserve-fund
  contribution as one line.
- **Schedule 2, Undercroft parking** (4 lines, subtotal £5,050).
- Total annual budget £35,370; the half-year on-account demand for all 18 leaseholders is £17,685.

Each flat's charge is the **apportionment percentage stated in its lease deed applied to the half-year
amount in each schedule separately**. Schedule 2 is shared only among the flats that have undercroft
parking, so a flat's Schedule 2 percentage differs from its Schedule 1 percentage and is zero for flats
with no parking.

### 1.1 Verified defects in the current model (as built)

Audited against the live schema and code on 2026-07-11:

- D1. `rtm_mgmt_budget_lines` stores each line as a row (good), but actual spend in `rtm_mgmt_payables`
  is correlated to a budget line by **exact text match on `(schedule, budget_line)`**, with no foreign
  key. A name mismatch silently mis-states variance. (`budget_actuals()`.)
- D2. There is **no `schedules` table**. `schedule` is a free text tag (`sch1`, `sch2`, ...) on several
  tables. Schedules are not first-class entities and carry no subtotal, basis or participation set.
- D3. `rtm_mgmt_flats` has a single `apportionment_band` text field. There is **no per-schedule
  percentage per flat**, and the band is **never used in any calculation**: demand amounts are entered,
  not derived. A two-schedule building cannot be correctly apportioned.
- D4. Neither the Cockpit snapshot nor the plugin exposes per-flat per-schedule apportionment, so
  reconciliation of "sum of Schedule 2 shares == Schedule 2 budget" is impossible and **credit notes
  cannot be computed** (an underspent Schedule 2 must credit only parking flats, each by their Sch2 ‰).

This specification removes D1 to D4.

---

## 2. Glossary and conventions

- **Schedule**: a named cost group with its own participation set and apportionment basis. Two are in
  scope (`sch1`, `sch2`); the model must generalise to N.
- **Permille (‰)**: integer parts per thousand. A flat's share of a schedule is an integer in [0, 1000].
  The participating flats' permille for a schedule **must sum to exactly 1000**.
- **Pence**: integer. Never floats for money. Formatting via existing `_money(pence)`.
- **Period**: a demand cycle, e.g. `2026-H2` (half-year ending 31 Dec 2026). A schedule has one budgeted
  amount per financial year; a period draws a fraction of it (a half-year draws 1/2).
- **Penny-exact**: the sum of derived per-flat pence for a schedule/period equals the schedule/period
  pence exactly, achieved by the largest-remainder method (section 6.3).

---

## 3. Numbered requirements

### 3.1 Schedules as first-class citizens
- R1. A `rtm_mgmt_schedules` table SHALL hold each schedule as a row with a stable code, display name,
  description, apportionment basis note, an `is_reserve_linked` flag, and an active flag.
- R2. Every budget line, payable, ledger entry and demand SHALL reference a schedule by its code, and
  that code SHALL exist in `rtm_mgmt_schedules` (enforced in the write path).
- R3. A schedule SHALL expose, per financial year, its budgeted total (sum of its budget lines) and its
  actual/committed spend, derived, not stored redundantly.

### 3.2 Budget lines and evidence correlation
- R4. `rtm_mgmt_budget_lines` SHALL gain a stable `id` used as the canonical key for correlation
  (already present) and SHALL be unique on `(year, schedule, budget_line)`.
- R5. `rtm_mgmt_payables` SHALL gain a nullable `budget_line_id` foreign key; spend-to-budget
  correlation SHALL use `budget_line_id` when present and fall back to the legacy `(schedule,
  budget_line)` text match only for un-migrated rows.
- R6. `rtm_mgmt_evidence` (polymorphic `target_type/target_id`) SHALL support `target_type='budget_line'`
  with `target_id = budget_lines.id`, and `target_type='payable'`, so an invoice/quote/receipt attaches
  to a specific line or payable.
- R7. `budget_actuals(year)` SHALL report, per line, budgeted/spent/committed/variance AND the count of
  correlated evidence items, computed via `budget_line_id`.

### 3.3 Per-flat per-schedule apportionment
- R8. A `rtm_mgmt_flat_apportionment` table SHALL store one row per `(flat_id, schedule)` with an integer
  `permille` in [0,1000] and provenance (source lease clause, effective date, note).
- R9. For every schedule that a flat participates in, a row SHALL exist; absence means the flat does not
  participate (equivalent to 0‰), and such flats SHALL NOT be demanded or credited for that schedule.
- R10. The system SHALL enforce, per `(schedule, effective set)`, that the sum of participating flats'
  permille equals exactly 1000; a set that does not sum to 1000 is INVALID and SHALL block demand
  generation for that schedule with a precise diagnostic.
- R11. The legacy `rtm_mgmt_flats.apportionment_band` SHALL be retained read-only for provenance and
  SHALL NOT be used in any calculation after migration (section 8).

### 3.4 Demand derivation
- R12. A demand run SHALL be defined by `(year, period, fraction)` and SHALL, for each schedule and each
  participating flat, derive the flat's charge as
  `flat_charge_pence = round_by_largest_remainder(schedule_period_pence, flats->permille)`
  where `schedule_period_pence = round(schedule_year_pence * fraction)` (section 6).
- R13. Each derived demand SHALL persist the schedule code AND the exact permille used, so the demand is
  self-explaining and independently recomputable/auditable.
- R14. The sum of all participating flats' derived charges for a schedule/period SHALL equal
  `schedule_period_pence` exactly (penny-exact, R-invariant checked in R19).
- R15. A flat's total demand for a period SHALL be the sum across schedules of its per-schedule charges;
  the demand document SHALL show the per-schedule breakdown, not just a single total.
- R16. Demand generation SHALL be idempotent per `(flat, period)`: re-running supersedes prior demand
  lines via the existing supersede/reversal ledger mechanism, never double-charging.

### 3.5 Reconciliation invariants (checkable, reportable)
- R17. Schedule budget integrity: for each schedule/year, `sum(budget_lines.amount_pence) ==
  schedule.budgeted_total` (definitional; the report SHALL surface any orphan line whose schedule code
  is not in `rtm_mgmt_schedules`).
- R18. Apportionment integrity: for each schedule, `sum(participating flats.permille) == 1000`.
- R19. Demand integrity: for each schedule/period, `sum(flat demands for that schedule) ==
  schedule_period_pence`.
- R20. Cross-schedule total integrity: `sum over schedules(schedule_period_pence) == published period
  total` (e.g. £17,685 for 2026-H2).
- R21. Spend vs budget: per line and per schedule, `variance = budgeted - spent - committed`; a schedule
  in aggregate deficit or surplus SHALL be flagged for a possible balancing charge or credit note.
- R22. A single `reconcile(year, period)` function SHALL return the pass/fail status of R17 to R21 with
  the exact offending rows and pence deltas.

### 3.6 Credit notes
- R23. A credit note SHALL be issuable at schedule granularity: given a schedule/year with actual+committed
  below the demanded-on-account amount, the creditable pool is `credit_pool_pence = demanded_pence -
  (actual_pence + committed_pence)` (never negative; a deficit yields a balancing charge instead, out of
  scope of the credit path).
- R24. Each participating flat's credit SHALL be `round_by_largest_remainder(credit_pool_pence,
  flats->permille)` using the SAME permille set that raised the demand, so credits reconcile to the pool
  exactly (penny-exact).
- R25. A credit note SHALL post a ledger CREDIT entry (`kind='credit_note'`) per flat, tagged with the
  schedule and a `credit_note_id`, reducing the flat's balance, and SHALL be immutable (corrections via a
  reversing entry, matching the existing ledger discipline).
- R26. A credit note SHALL be reproducible as a branded PDF per flat (reuse the RTM templating engine) and
  the run SHALL be summarised for the director (total credited, per-flat list).
- R27. Credit notes SHALL only ever touch flats with `permille > 0` for that schedule; a non-parking flat
  SHALL never receive a Schedule 2 credit.

### 3.7 Exposure to Mongoose Cockpit and the WordPress plugin
- R28. The director summary (`director_summary`) SHALL gain a `schedules` array: per schedule, its
  budgeted/actual/committed/variance, participation count, apportionment-integrity status, and any
  creditable/deficit pool for the current period.
- R29. The portal snapshot SHALL gain, per flat, a `schedule_apportionment` array of `{schedule, name,
  permille, period_charge_pence, period_charge}` and a `credit_notes` array of `{schedule, amount,
  issued_date, credit_note_id}`.
- R30. The plugin SHALL render, in the leaseholder portal, the per-schedule breakdown of a flat's charge
  and any credit notes; and in the director Financials page, the per-schedule budget/actual/variance and
  apportionment-integrity state. All read-only (Mongoose remains the ledger and source of truth).
- R31. No sensitive field (per section 8 exclusions of the existing snapshot) SHALL be added; permille and
  per-schedule amounts are leaseholder-appropriate and permitted.

### 3.8 Non-functional
- R32. All schema changes SHALL be additive and idempotent (migration guarded by a `db_version` marker),
  safe to run on the live pre-handover brain (currently empty of flats/demands).
- R33. All money maths SHALL use integer pence and the largest-remainder allocator; no floating point in
  any monetary path.
- R34. Every new function SHALL be covered by targeted tests (section 10), runnable without spawning a
  server on port 8765.

---

## 4. Target schema (DDL)

New and altered tables. Existing tables keep their columns; additions are called out. SQLite dialect.

### 4.1 `rtm_mgmt_schedules` (new)
```sql
CREATE TABLE IF NOT EXISTS rtm_mgmt_schedules (
    code            TEXT PRIMARY KEY,          -- 'sch1', 'sch2', ...
    name            TEXT NOT NULL,             -- 'Building service costs'
    description     TEXT NOT NULL DEFAULT '',
    apportionment_basis TEXT NOT NULL DEFAULT '', -- free note: how the lease shares this schedule
    is_reserve_linked INTEGER NOT NULL DEFAULT 0, -- 1 if this schedule funds the reserve
    sort_order      INTEGER NOT NULL DEFAULT 0,
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
```
Seed rows: `('sch1','Building service costs',...,is_reserve_linked=1)`, `('sch2','Undercroft parking',...)`,
plus the existing tags (`reserve`, `one_off`, `company_admin`, `unknown`) as inactive-or-utility rows so
existing data never orphans (R17 diagnostic).

### 4.2 `rtm_mgmt_flat_apportionment` (new)
```sql
CREATE TABLE IF NOT EXISTS rtm_mgmt_flat_apportionment (
    flat_id         TEXT NOT NULL,
    schedule        TEXT NOT NULL,             -- FK -> rtm_mgmt_schedules.code
    permille        INTEGER NOT NULL,          -- 0..1000, parts per thousand of this schedule
    source_clause   TEXT NOT NULL DEFAULT '',  -- lease clause / deed reference (provenance)
    effective_from  TEXT NOT NULL DEFAULT '',
    note            TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (flat_id, schedule),
    CHECK (permille >= 0 AND permille <= 1000)
);
CREATE INDEX IF NOT EXISTS idx_flat_appn_schedule ON rtm_mgmt_flat_apportionment(schedule);
```
Absence of a `(flat, schedule)` row == 0‰ == non-participation (R9).

### 4.3 `rtm_mgmt_budget_lines` (altered)
Already has `id, year, schedule, budget_line, amount_pence, ...`. Add:
```sql
-- enforce a stable natural key for correlation
CREATE UNIQUE INDEX IF NOT EXISTS uq_budget_line ON rtm_mgmt_budget_lines(year, schedule, budget_line);
```

### 4.4 `rtm_mgmt_payables` (altered)
Add a nullable FK to the budget line for robust correlation (R5):
```sql
ALTER TABLE rtm_mgmt_payables ADD COLUMN budget_line_id INTEGER;   -- nullable; -> budget_lines.id
CREATE INDEX IF NOT EXISTS idx_payable_budget_line ON rtm_mgmt_payables(budget_line_id);
```

### 4.5 `rtm_mgmt_demands` (altered)
Already has `schedule`. Add the exact apportionment used, for self-audit (R13):
```sql
ALTER TABLE rtm_mgmt_demands ADD COLUMN permille INTEGER;          -- ‰ used to derive amount_pence
ALTER TABLE rtm_mgmt_demands ADD COLUMN period_fraction TEXT;      -- e.g. '1/2'
ALTER TABLE rtm_mgmt_demands ADD COLUMN derivation_run_id TEXT;    -- groups a demand run
```

### 4.6 `rtm_mgmt_credit_notes` (new) and ledger
```sql
CREATE TABLE IF NOT EXISTS rtm_mgmt_credit_notes (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,             -- groups a credit-note batch
    flat_id         TEXT NOT NULL,
    schedule        TEXT NOT NULL,
    year            TEXT NOT NULL,
    period          TEXT NOT NULL,
    permille        INTEGER NOT NULL,
    pool_pence      INTEGER NOT NULL,          -- schedule creditable pool at issue time
    amount_pence    INTEGER NOT NULL,          -- this flat's credit
    reason          TEXT NOT NULL DEFAULT '',
    pdf_key         TEXT,
    issued_date     TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_credit_flat ON rtm_mgmt_credit_notes(flat_id);
```
Credit posts to the existing `rtm_mgmt_ledger_entries` with `kind='credit_note'`,
`credit_pence=amount_pence`, `schedule=<schedule>`, `document_number=<credit_note_id>` (R25).

### 4.7 `rtm_mgmt_flats` (unchanged columns)
`apportionment_band` retained read-only for provenance (R11); not read by any calc post-migration.

---

## 5. Reference data for Example Court (seed / import target)

Schedule 1 lines (annual £): Buildings insurance 6,100; Directors' and officers' insurance 270; Gutter
cleaning 860; Window cleaning 390; Cleaning 3,640; Exterior wall cleaning 960; Management and
administration 5,780; Accounts fee 540; Call routing and web portal 130; Stationery and postage 100; Fire
risk assessment 470; Emergency light testing 540; Communal electricity 1,280; Entry phone maintenance
270; Lift maintenance 860; Lift telephone 370; Lift engineering inspection 1,120; Lift engineering
insurance 160; Building condition survey 1,770; Reserve fund contribution 4,710. Subtotal 30,320.

Schedule 2 lines (annual £): Gate maintenance 640; Pump maintenance 540; Undercroft clearout 210; Steel
caging for bike storage area 3,660. Subtotal 5,050. Total 35,370. Half-year (fraction 1/2) 17,685.

Per-flat permille for `sch1` (all 18 flats, sum 1000) and `sch2` (parking flats only, sum 1000) come from
the lease deeds and are imported into `rtm_mgmt_flat_apportionment`. Until supplied, the apportionment set
is INVALID (R10) and demand generation is blocked with a clear message, by design.

---

## 6. Mechanics (exhaustive)

### 6.1 Schedule period amount
For schedule `s`, financial year `y`, period fraction `f = p/q` (half-year `1/2`):
```
schedule_year_pence[s]   = sum(amount_pence of budget lines where year=y and schedule=s)
schedule_period_pence[s] = round_half_up(schedule_year_pence[s] * p / q)
```
`round_half_up` uses Decimal to avoid float drift. For Example Court sch2: 505000 * 1 / 2 = 252500p
exactly; sch1: 3032000 * 1/2 = 1516000p; total 1768500p = £17,685.00 (matches R20).

### 6.2 Participation set and validity
```
participants[s] = { flat : row exists in flat_apportionment with schedule=s and permille>0 }
valid[s]        = ( sum(permille over participants[s]) == 1000 )
```
If not `valid[s]`, demand/credit for `s` is refused with the offending sum and the flat list (R10).

### 6.3 Largest-remainder allocation (penny-exact) — the core allocator
Given a `total_pence` and a map `flat -> permille` (participants, summing to 1000), allocate integer pence
so the parts sum to exactly `total_pence`:

```
1. For each flat i: exact_i = total_pence * permille_i           (integer, may be large)
                    base_i  = exact_i // 1000                    (floor division)
                    rem_i   = exact_i %  1000                    (remainder in [0,999])
2. allocated = sum(base_i)
3. shortfall = total_pence - allocated                          (# of pennies still to place, >= 0)
4. Order flats by rem_i DESC, tie-break by flat_id ASC (deterministic, reproducible).
5. Give +1 penny to the first `shortfall` flats in that order.
6. share_i = base_i (+1 if selected).
```
Because `sum(permille)=1000`, `sum(exact_i) = total_pence * 1000`, so `sum(base_i) = total_pence -
shortfall` with `0 <= shortfall < N`; step 5 restores the exact total. Determinism (step 4 tie-break)
guarantees the same input always yields the same pennies, so re-runs and independent recomputation agree.

Worked example. `total_pence = 100003`, three parking flats A/B/C at permille 334/333/333:
```
exact: A=33401002 B=33300999 C=33300999
base : A=33401    B=33300    C=33300     -> allocated 99001? no: 33401+33300+33300 = 100001
rem  : A=2        B=999      C=999
shortfall = 100003 - 100001 = 2
order by rem desc, flat_id asc: B(999), C(999), A(2)
+1 penny to B and C -> A=33401, B=33301, C=33301  ; sum = 100003  (exact)
```

### 6.4 Demand derivation run
```
run_id = new id
for each schedule s with valid[s]:
    sp = schedule_period_pence[s]
    shares = largest_remainder(sp, participants[s].permille)
    for each flat, share_i:
        supersede any prior demand line for (flat, period, s)   # reversal ledger entry (R16)
        create_demand(flat, period, schedule=s, amount_pence=share_i,
                      permille=permille_i, period_fraction=f, derivation_run_id=run_id)
        post ledger demand entry (debit_pence=share_i, schedule=s, document_number=demand#)
assert per-schedule: sum(shares) == sp                          # R19, hard assert
assert total: sum over s(sp) == published_period_total          # R20
```
Each flat's period demand document aggregates its per-schedule lines (R15).

### 6.5 Budget-to-actual correlation
```
for each budget line bl:
    spent     = sum(payables.paid_amount_pence where status='Paid'
                    and (budget_line_id = bl.id
                         or (budget_line_id is null and schedule=bl.schedule and budget_line=bl.name)))
    committed = same, status in ('Scheduled','Approved')
    variance  = bl.amount_pence - spent - committed
    evidence  = count(rtm_mgmt_evidence where target_type='budget_line' and target_id=bl.id)
```
Roll up to schedule totals for R21.

### 6.6 Credit note run
```
for a schedule s, year y, period p:
    demanded  = schedule_period_pence[s]                        # what was demanded on account
    actual    = spent[s] + committed[s]                         # scaled to the period if annual (see note)
    pool      = max(0, demanded - actual)                       # creditable (R23); if <0 -> balancing charge (out of scope here)
    if pool == 0: no credit
    credits   = largest_remainder(pool, participants[s].permille)   # SAME permille that raised the demand (R24)
    run_id = new id
    for each flat, credit_i (credit_i may be 0 for a small pool; skip zero):
        cn = insert rtm_mgmt_credit_notes(run_id, flat, s, y, p, permille_i, pool, credit_i, ...)
        post ledger credit entry (kind='credit_note', credit_pence=credit_i, schedule=s, document_number=cn.id)  # R25
        render branded credit-note PDF (R26); store pdf_key
    summarise run for director (R26)
```
Note on period scaling: the creditable comparison is like-for-like within the period. If actuals are held
annually, the engine scales the annual budget by `f` for the "demanded on account" side and compares to
period-scoped actuals; the exact policy (true-up at year end vs mid-year interim credit) is a config flag
`CREDIT_POLICY in {year_end, interim}` defaulting to `year_end`.

### 6.7 Reserve fund
The reserve contribution is a Schedule 1 budget line with `is_reserve_linked` on the schedule. Reserve
money demanded is apportioned by the sch1 permille like any other line; it flows to the reserve fund in
the bookkeeper (`reserve_fund_trust`) rather than being creditable in-year. The credit engine EXCLUDES
reserve-linked amounts from the creditable pool unless `CREDIT_POLICY=interim` explicitly includes them.

---

## 7. API surface (Cockpit-nonce or token gated)

- `POST /rtm/schedules/upsert` — create/update a schedule.
- `GET  /rtm/schedules` — list schedules with derived budget/actual per year.
- `POST /rtm/apportionment/upsert` — set a flat's permille for a schedule.
- `POST /rtm/apportionment/import` — bulk import per-flat per-schedule permille (CSV/JSON).
- `GET  /rtm/apportionment/validate?year=&period=` — R18/R10 status per schedule.
- `POST /rtm/demands/derive` — run 6.4 for `(year, period, fraction)`; returns per-flat per-schedule lines.
- `GET  /rtm/reconcile?year=&period=` — R22 report (R17..R21) with offending rows.
- `POST /rtm/credit-notes/run` — run 6.6 for `(schedule, year, period)`; returns the batch summary.
- `GET  /rtm/credit-notes?flat=` — list issued credit notes.
- Existing `/rtm/director/summary` extended per R28; `/rtm/portal/publish` snapshot extended per R29.

---

## 8. Migration plan (off `apportionment_band`)

1. Bump an `rtm_mgmt_apportionment_db_version` marker; create the new tables and add columns idempotently.
2. Seed `rtm_mgmt_schedules` with `sch1`, `sch2` (+ utility rows for legacy tags).
3. Backfill `budget_lines` unique index; backfill `payables.budget_line_id` by best-effort exact
   `(schedule, budget_line)` match, logging any that do not resolve (surfaced by R7/R17).
4. Do NOT auto-convert `apportionment_band` to permille (it is a single value and cannot be split across
   two schedules safely). Instead, require the per-schedule permille import (section 5). Until imported,
   `validate` reports INVALID and `derive` is blocked, by design, so nothing is mis-billed.
5. Leave `apportionment_band` in place, read-only, as provenance.

The migration is safe on the current brain because there are zero flats/demands/budget rows pre-handover;
on populated data it is additive and non-destructive.

---

## 9. Exposure schema deltas

Director summary gains:
```jsonc
"schedules": [ { "code":"sch1","name":"Building service costs","budgeted":"£30,320.00",
  "spent":"£...","committed":"£...","variance":"£...","participants":18,
  "apportionment_ok":true,"period_demanded":"£15,160.00","creditable":"£0.00" } ]
```
Snapshot per flat gains:
```jsonc
"schedule_apportionment": [ { "schedule":"sch1","name":"Building service costs","permille":56,
  "period_charge":"£848.96" }, { "schedule":"sch2","permille":0,"period_charge":"£0.00" } ],
"credit_notes": [ { "schedule":"sch2","amount":"£12.34","issued_date":"2027-01-15","credit_note_id":"cn_..." } ]
```

---

## 10. Test plan (targeted, no server on 8765)

- T1. `largest_remainder`: sums exactly for random totals/permille; deterministic; worked example 6.3.
- T2. Schedule period amount: Example Court sch1/sch2 halves == 1516000/252500; total 1768500.
- T3. Apportionment validity: a set summing to 999 or 1001 is INVALID; 1000 valid; missing rows == 0‰.
- T4. Demand derivation: per-schedule sum == schedule_period_pence (R19); total == R20; re-run supersedes,
  no double charge (R16).
- T5. Budget correlation: FK match preferred; legacy text fallback; evidence count per line.
- T6. Credit note: pool computed per R23; credits sum to pool (R24); non-participants get nothing (R27);
  ledger credit posted; balance reduced.
- T7. Reconcile report: R17..R21 pass on a valid fixture; each invariant fails loudly on a seeded breakage.
- T8. Snapshot/summary exposure: fields present, no excluded field leaks (existing exclusion gate).
- T9. Migration idempotency: run twice, no error, columns/tables present once.

---

## 11. Build sequencing

1. Spec (this document).
2. Schema + migration + `rtm_mgmt_schedules`/`flat_apportionment` stores and CRUD.
3. `largest_remainder` allocator + unit tests (T1).
4. Apportionment validity + `validate` (T3).
5. Demand derivation engine + supersede + tests (T2, T4).
6. Budget correlation by id + evidence count (T5).
7. `reconcile(year, period)` (T7).
8. Credit-note engine + ledger posting + PDF (T6).
9. Cockpit surfaces (schedules, apportionment editor/import, reconcile view, credit-note run).
10. Snapshot + director-summary exposure; plugin per-schedule + credit-note rendering (T8).
11. Migration on the live brain; import Example Court permille when the lease figures are supplied.

---

## 12. Assumptions and open questions

- A1. Per-flat per-schedule permille come from the lease deeds; they are input, not derivable. Needed
  before real demands can be generated (blocked-by-design until then).
- A2. Credit policy default `year_end`; confirm whether interim (mid-year) credits are wanted.
- A3. Whether reserve contributions are ever creditable in-year (default: no).
- A4. Period model: half-year `2026-H2` with fraction `1/2`; confirm the financial-year boundary (30 June)
  for year tagging of budget lines.
