# mongoose-core

The non-assistant half of Mongoose: everything a residents' Right to Manage (RTM)
company needs, plus the unified search and document engine it is built on. It runs on its own, or
underneath `mongoose-assistant`, which adds the chat assistant, Cockpit, devices and home control.

## What is in here

- **RTM management**: leaseholder register, service charge demands and receipts, two-schedule
  apportionment, payment plans, overdue notices, scheduled half-year demands, on-account credit,
  registers (building, insurance, fire risk, lease terms), seller packs (LPE1), member votes,
  governance and company secretary records.
- **Money**: a trust-account cash book, fund accounts, Metro bank feed import and exact
  reconciliation, expense mapping, payee bank-detail fraud guard, accountant export.
- **Documents**: branded Word and PDF generation from templates (demands, receipts, letters,
  overdue notices), cover emails, document intake, analysis and evidence-backed totals.
- **Unified search**: one index over email, files, attachments and voicemail, with OCR, content
  extraction and optional embeddings (SQLite FTS5).
- **WordPress portal bridge**: the leaseholder portal plugin (`wordpress-plugin/`), the plugin
  source generator (`services/wp_plugin.py`), the WordPress connector, and the HTTP API the plugin
  talks to (`mongoose/rtm_api.py`): portal sync, snapshots, demand and receipt PDFs, sends,
  announcements, access requests and signed download links.

## Providers supported

- **Banking:** Metro Bank statement import. Metro offers no scheduled export, so you export a CSV and drop
  it in a watched folder: `services/metro_watch.py` picks it up, imports it with duplicate protection,
  reconciles exact matches against demands and queues the rest for review. Mongoose never logs in to the
  bank, never connects to it and never moves money.
- **Bank feed playbook:** `services/rtm_bank_feed_playbook.py` (status facade in `services/metro_ob.py`)
  helps you choose and test a feed route per account type, records the tests, consent dates and evidence,
  and exports an audit pack. It covers Xero direct feeds, Crunch, Pandle, ClearBooks and open-banking
  (AIS) providers: Plaid, TrueLayer, GoCardless, Moneyhub, Yapily and Finverse. Consent and the
  connection itself stay with the provider you choose.
- **Accounting:** the accountant export (`services/rtm_export.py`) renders the same figures as Xero,
  QuickBooks Online and generic bank-statement CSVs, QIF, OFX, an XLSX workbook, a PDF report and JSON,
  bundled with the source documents.
- **Voicemail:** VoIP provider notification emails (caller, mailbox and audio extracted), Home Assistant
  voicemail staging, and a generic fallback. Set `MONGOOSE_VOICEMAIL_SENDER` to your provider's
  notification address. No provider account details ship in the code.
- **Portal:** WordPress, via the bundled leaseholder portal plugin.

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[test,content,rtm_docs]'
uvicorn mongoose.core_app:app --port 8765
curl -s localhost:8765/health
```

Data lives outside the code: `MONGOOSE_DB_PATH` (SQLite) and `MONGOOSE_DATA_DIR`. All packaged RTM
data is fictional (Example Court, Anytown, Brian Bennett, Sample Bank). A real company's register,
identity and letterhead go in `<data_dir>/rtm/` and are loaded at runtime; see `docs/RTM_CONFIG.md`.

## Site profile

Every per-install value (owner name, site domain, RTM company name and slug, the block and flat-code
scheme, the WordPress REST namespace and plugin slug, the voicemail provider, private word lists) comes
from `mongoose.profile`, never from code. The packaged `assets/profile.default.json` is fictional; a real
install puts its own `profile.json` next to the brain (`<data_dir>/profile.json`, `chmod 600`). Missing
or unknown keys fail loud, and no `.env` file can repoint the profile. User-facing copy uses
`{{site.domain}}`-style placeholders filled by `profile.render()`.

## With the assistant

`mongoose-assistant` depends on this package. Both install into the same `mongoose` import namespace,
so `from mongoose.services.rtm_portal import ...` works either way, and the assistant mounts
`mongoose.rtm_api.router` inside its own app. Release both at the same version.

## Tests

```bash
pip install -e '.[test]'                              # core only
pip install -e '.[test,content,rtm_docs,embeddings]'  # everything
python -m pytest -q
```

Measured in a fresh virtual environment with core alone installed:

| Install | Passed | Skipped | Failed |
|---|---|---|---|
| `[test]` | 394 | 5 (Word and PDF document tests need the `content` / `rtm_docs` extras) | 0 |
| all extras | 398 | 1 (the RTM knowledge base is not installed in a fresh setup) | 0 |

RTM journey tests that drive the full assistant app live in mongoose-assistant.

## Privacy

`tools/leak_gate.py` blocks commits that would publish personal data (private term list kept outside
the repo, identifier patterns, and human review of any line about a person's body, health, mind,
family or routine). Install: `ln -s ../../scripts/pre-commit-leak-gate.sh .git/hooks/pre-commit`.

## License

MIT.
