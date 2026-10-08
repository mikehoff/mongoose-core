# Voicemail integration

Mongoose supports two parallel voicemail ingestion paths that converge into one canonical record per call.

## Sources

### Pi/HA staging (primary)

The VoIP provider HA integration writes a JSONL staging file on the Pi at `/config/claude_bus_staging/mongoose_voicemail.jsonl`. Each line is a Protocol v3 envelope with a nested `body`:

```json
{"msg_id": "vm_abc123", "from": "ha/example_court", "account_number": "30000000",
 "voicemail_at": "2026-06-24T14:30:00+01:00",
 "body": {"uid": "abc123", "caller": "07700900123",
          "transcript": "Hi Sam, please call back about the documentation.",
          "wav_rel_path": "/media/voicemail_abc123.wav"}}
```

The transcript here is from HA's own STT (whatever is configured in Home Assistant). It arrives before IMAP and is stored immediately as `transcript_source="ha_staging"`, `transcript_status="done"`.

Mongoose drains this file via `drain_voicemail()`, normalises the nested `body` into flat fields, merges top-level envelope metadata, then calls `index_voicemail_from_protocol_staging()`.

### IMAP (secondary)

VoIP provider emails a notification to the configured IMAP mailbox. Subject format: `New message in mailbox for master group of <account_number> from <caller_number>`. A WAV attachment is included.

Mongoose detects this via `_is_voicemail_notification()` (checks sender domain `voip.example` and subject pattern). Both `sync_imap()` and `sync_imap_incremental()` call `index_voicemail_from_email()` to promote the email into a voicemail record.

## Reconciliation

Both paths use `canonical_identity_key(account_number, caller_number, voicemail_at)` to produce a stable identity fingerprint. When both arrive for the same call, `_upsert_voicemail()` merges them into one row — the HA transcript is kept, the IMAP audio attachment reference is added.

## Transcript quality hierarchy

When two machine-generated transcripts exist for the same call, the LLM judges which is better:

- `manual` — always wins without AI review
- `local_command` — local Whisper; replaces `ha_staging`
- `ha_staging` — HA's STT result; replaced by Whisper if configured

The LLM prompt asks it to prefer completeness, correct proper nouns, and coherent punctuation. The outcome is stored in `transcript_source` (e.g. `ha_staging:ai_kept`, `local_command:ai_chosen`, `ai_merge:ha_staging+local_command`) and a unified diff is stored in `raw_json.transcript_diff`.

## Whisper

Whisper is auto-detected from PATH (no configuration needed). Run `mongoose setup-voice` or use the cockpit Settings → Voice transcription → Install to install it. Once installed, Whisper automatically transcribes IMAP WAV attachments. The cockpit Settings also shows whether Whisper + ffmpeg are installed.

## Searching

Voicemails are indexed into SQLite FTS5. The `search_voicemails` tool (chat: "what voicemails do I have from last week?") searches by transcript, caller number, and date. The `/voicemails` and `/voicemails/search` endpoints serve the cockpit.
