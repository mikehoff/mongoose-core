# How Mongoose crawls your corpus

The job of the crawler is to quietly maintain a complete, current view of your
files, email, and attachments without you babysitting it. It separates two very
different jobs that the old periodic re-scan conflated:

- **Backfill** (historical): build the corpus once. Slow, resumable, paginated.
  "All my files under this root", "all my mail". Tracked by a cursor so a restart
  resumes where it stopped instead of starting over.
- **Freshness** (incremental): fetch only what changed since last time. Frequent and
  cheap. New mail since the last UID, files changed since the last walk. This is
  what runs on a short interval.

## Why this is better than the old re-scan

Before, the email job did `SEARCH ALL` then took the last 50 IDs, so it re-fetched
the same recent messages every cycle and never saw older mail. The file job
re-walked the whole tree every interval. That's wasteful and it never builds a
complete historical corpus.

Now each source carries a **cursor**:

- **IMAP**: a UID watermark. Freshness does `UID SEARCH <last_uid+1>:*` and fetches
  only genuinely-new messages, honouring UIDVALIDITY (if the mailbox renumbers, the
  cursor resets). Verified by test: after seeing UID 102, the next pass fetches
  nothing rather than re-pulling 50 messages.
- **Files**: a path cursor. The walk runs in sorted path order, resuming strictly
  after the last path handled, in bounded chunks. mtime-skipping means unchanged
  files cost almost nothing.

## Load controls

A backfill must never hog the machine. Every run is bounded and resumes next tick:

```
MONGOOSE_CRAWLER_MAX_SECONDS_PER_RUN=120     # wall-clock budget per run
MONGOOSE_CRAWLER_MAX_FILES_PER_RUN=1000      # file cap per run
MONGOOSE_CRAWLER_MAX_MESSAGES_PER_RUN=250    # message cap per run
MONGOOSE_CRAWLER_MIN_FREE_DISK_GB=5          # skip when disk is low
```

A run that hits its budget is marked `partial` and the cursor is persisted, so the
next scheduler tick picks up exactly where it left off.

## Status and control

```
mongoose crawler --status              # per-source progress and cursors
mongoose crawler --freshen imap        # incremental IMAP pass now
mongoose crawler --freshen /path/root  # incremental file pass now
mongoose crawler --backfill /path/root # a backfill chunk now
```

`--status` shows lines like:

```
files backfill complete: 12,840 scanned, 12,840 indexed; last run 02:10
imap backfill in progress: 4,812 scanned, 930 indexed; fresh cursor 18492
```

## What's done and what's still a seam (honest state)

Done and tested offline:
- backfill/freshness split with per-source cursors and `crawl_sources` / `crawl_runs`
  state tables
- IMAP cursor-based freshness (UID watermark + UIDVALIDITY handling)
- resumable, budget-bounded file walking
- load controls (time/item caps, low-disk skip) and status visibility
- the scheduler's email job now uses the IMAP cursor instead of last-50

Deliberately left as clearly-marked seams (they need live provider APIs that can't
be meaningfully tested without real credentials, and would be guesswork to "finish"
blind):
- **Gmail** provider-native freshness via `historyId` and backfill via `pageToken`.
  Today Gmail freshness uses a `newer_than` time window, which is correct but not as
  cheap as a history cursor.
- **Microsoft Graph** `deltaLink` freshness and `nextLink` backfill. Today it pulls a
  recent page.
- **macOS FSEvents / watchdog** event-based file watching. Today files use periodic
  resumable reconciliation, not push events.
- **Android push-agent changed-files mode** (send a delta, not the whole tree).

The honest summary: the architecture is now a real backfill + freshness system with
cursors, resumability, and load control, and the two sources I can fully implement
and test offline (IMAP, files) use it. The cloud-provider-native cursors and OS-level
file events are plumbed-for but not wired, because doing them blind without live API
behaviour would be the same "looks done, isn't" gap the audit rightly flagged. Those
are the next crawler patch, against real accounts.
