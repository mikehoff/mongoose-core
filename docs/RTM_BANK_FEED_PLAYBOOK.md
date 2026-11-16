# RTM Metro bank-feed playbook

Mongoose now has a dedicated RTM Metro bank-feed verification layer. It is based on the current research guide and is deliberately **read-only**: it records the account inventory, provider test steps, evidence, support messages and outcome register, but it never connects directly to Metro and never moves money.

## Why this exists

Metro support cannot be tested in the abstract. The RTM has to identify the exact product and online-banking platform for each account first, because the service-charge account and reserve account may have different eligibility. The playbook therefore starts with an account inventory, then routes provider tests according to product type.

## Provider order

The implemented order is:

1. Xero direct feed
2. Crunch Plus
3. Plaid
4. Pandle Pro
5. GoCardless Bank Account Data
6. Moneyhub
7. Yapily
8. Clear Books Small
9. TrueLayer
10. Finverse

Each provider entry contains prerequisites, exact test steps, verification checks, success/failure criteria, evidence to capture and RTM risk notes.

## Account types tracked

- Community Current Account
- Community Instant Access Deposit Account
- Trust Bank Account
- Business Bank Account
- Commercial Current Account
- Unknown / not yet confirmed

The service flags Xero as a strong first test for eligible current-account products and as the wrong first test for Community Instant Access Deposit reserve accounts unless Metro confirms otherwise in writing.

## Evidence pack

The export creates:

- `rtm-bank-feed-playbook.md`
- `rtm-bank-feed-playbook.json`
- `support-message-templates.md`
- `account-inventory.csv`
- `provider-test-register.csv`
- `evidence-register.csv`
- empty folders for Metro product proof, provider setup, test runs and support correspondence

## API routes

Read routes:

- `GET /rtm/bank-feed/status`
- `GET /rtm/bank-feed/playbook`
- `GET /rtm/bank-feed/playbook?provider=xero`
- `GET /rtm/bank-feed/messages`
- `GET /rtm/bank-feed/messages/{key}`

Write/export routes, nonce or token protected:

- `POST /rtm/bank-feed/accounts`
- `POST /rtm/bank-feed/tests`
- `POST /rtm/bank-feed/evidence`
- `POST /rtm/bank-feed/export-pack`

## Chat/tool access

The read-only `rtm_bank_feed_playbook` tool lets Mongoose answer questions such as:

- Which Metro bank feed candidate should we test first?
- Why is Xero not the right route for the reserve account?
- What evidence do we still need for Crunch?
- Give me the message to Metro/Crunch/Yapily.

`system_status(area="rtm_bank_feeds")` also returns the account inventory, provider order and next actions.

## Safety boundary

The playbook keeps the statutory trust-fund boundary explicit:

- service-charge and reserve accounts stay separate;
- access is account-information/read-only only;
- no payment initiation and no variable recurring payments are enabled by Mongoose;
- consent/revocation evidence is captured in the evidence pack.