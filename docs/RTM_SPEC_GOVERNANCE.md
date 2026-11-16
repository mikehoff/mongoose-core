# RTM spec governance: where the truth lives, and how building works

> Companion to `docs/RTM_BUILD_SOURCE_OF_TRUTH.md`, which is the canonical map of the two RTM layers
> (operations vs product build) and their stores. This document adds the governance rule that decides
> precedence when a brief, the tests, and a doc disagree. Where the two overlap, the source-of-truth
> map is authoritative on *where state lives*; this is authoritative on *which artefact wins*.


This document exists because an audit found there was no single declared source of truth for the
Example Court RTM requirements, and it was unclear how RTM building relates to the chat surface, the
cockpit build tab, and the orchestrating Claude thread. This declares all of it. It is itself now
part of the canonical record.

## 1. The single source of truth for RTM requirements

The canonical RTM requirements live, in priority order:

1. **The layered build brief** for a given RTM feature, as handed to Mongoose by the orchestrating
   Claude thread (for example the receipts-and-trust-reconciliation brief). The brief's Layer 2 is
   the specification; its acceptance tests are the contract. While a feature is being built, the
   brief is the truth.

2. **The committed acceptance tests in `tests/`** once the feature is built. After a build lands,
   the tests are the durable, executable source of truth: if the code and a prose doc disagree, the
   tests decide, because they are what actually runs. For the trust work these are
   `tests/test_rtm_trust_*.py`.

3. **The feature docs in `docs/`** (`RTM_MANAGEMENT.md`, `RTM_PORTAL_SPEC.md`, and the
   `MONGOOSE_FULL_SPEC.md` RTM sections) as the human-readable description. These must be kept in
   step with the tests; when they drift, the tests win and the doc is wrong.

`REQUIREMENTS_TRACEABILITY.md` maps conversation requirements to code. It is a traceability index,
not a forward spec, and is not the place to add new RTM requirements.

**Owner of the RTM spec:** the director (the human). New RTM requirements enter via a layered brief,
get built with acceptance tests, and the tests become canonical. No requirement is "real" until it
has an acceptance test.

## 2. How the requirements sync with the orchestrating Claude thread

The orchestrating thread holds the brief in its own context and hands it down. After a build:

- The build state of record is the chain of shipped zips plus the bootstrap primer. That is a
  *build-state* record (what is in the code now), not a *requirements* record.
- The requirements record of what was asked for is the **brief plus the acceptance tests it
  produced**. The tests travel with the code in every zip, so the requirements cannot be lost even
  if the originating chat is gone.

Sync rule: when a new RTM brief is built, its Layer 2 spec and acceptance criteria are encoded as
tests, and a short note is added to the relevant `docs/RTM_*.md`. That note plus the tests are how a
future session (or a different Claude thread) recovers what was intended without needing the original
conversation.

## 3. The three "build" surfaces, and which does what

These are genuinely different and must not be conflated:

- **Build PLANS** (`build_plans.py` + `builder.py`: `create_plan` / `approve_plan` / `run_plan`,
  exposed as the `mongoose build-plan-example-court-*` CLI commands). These scaffold and run a
  multi-step engineering plan for a feature. They are **CLI / orchestrator only**. They are gated:
  create, then approve, then run. They are NOT reachable from chat and are NOT what the cockpit
  "build" tab does.

- **Build / deploy ARTIFACTS** (`/build/stage`, `/build/deploy`, `/cockpit/update-stage/*`, the
  updater). This is the cockpit "build" tab and the updater: staging and applying a zip *update of
  Mongoose itself*. Apply is human-gated. This ships new Mongoose versions; it does not build RTM
  features.

- **CHAT**. The chat brain can READ RTM state (e.g. `tool_rtm_statement`) and can PROPOSE gated
  sensitive actions, which return a confirmation token and do nothing until the user confirms
  (`propose_sensitive_action` / `confirm_sensitive_action`). Chat **cannot create or run a build
  plan, and cannot deploy**. Its only writes are confirmation-gated proposals.

So: **RTM feature-building happens through build plans on the CLI, driven by a brief.** It is not a
chat action and not a cockpit build-tab action. The cockpit build tab is for shipping Mongoose
updates. Chat is for reading state and proposing gated everyday actions. If someone expects to "build
RTM from chat" or "from the build tab", that expectation is the misunderstanding; this is the answer.

## 4. Protocol V3 and RTM: do they touch?

Protocol V3 is the three-agent Home Assistant coordination (Chat, Code, Mongoose agents on the Pi).
RTM is local to Mongoose. As built today they are **independent**: RTM reconciliation, receipts, and
the trust model run entirely within Mongoose and do not flow through the three-agent protocol. If a
future requirement wants RTM events to coordinate across the three agents, that is a new brief with
its own acceptance tests, and this document should be updated to record the boundary changing. Until
then, the answer to "how do V3 peers integrate with RTM" is: they do not, by design.

## 5. The one rule that keeps this true

A requirement is real only when it has an acceptance test. A spec doc without a test is a wish; a
test without a doc is at least enforceable. Keep both, let the test win, and the truth cannot rot
silently between sessions or between threads.
