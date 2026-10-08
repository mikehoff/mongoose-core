# Mail sync (real data into the brief and search)

Mongoose syncs mail from IMAP, Gmail, or Microsoft. The sync engines and the scheduled
job already exist; the commands below let you SET UP a provider, prove it's working, and
see freshness, so the brief and search run on real data.

## See what's wired

```
mongoose mail status
```

Shows each provider as not-set-up / configured / authorized, with the last sync time and
a hint for the next step if it isn't syncing yet.

## Set up a provider

IMAP (any mail host):

```
export MONGOOSE_IMAP_HOST=imap.your-host.com
export MONGOOSE_IMAP_USERNAME=you@your-host.com
export MONGOOSE_IMAP_PASSWORD=...        # app password if your host needs one
mongoose mail sync
```

Gmail (OAuth):

```
# put your Google client-secrets JSON where settings.google_client_secrets_path points
mongoose authorize google                # opens consent in the browser
mongoose mail sync
```

Microsoft / Outlook (OAuth):

```
export MONGOOSE_MS_CLIENT_ID=...
mongoose authorize microsoft
mongoose mail sync
```

## Sync now, and keep it fresh

```
mongoose mail sync     # runs ONE cycle now and reports new messages
```

`mail sync` runs the exact same job the scheduler runs in the background, so once a
provider is authorized, install the unattended scheduler (see docs/SERVICE.md) and it
stays fresh on its own.

## See it in the Cockpit

The Overview tab has a Mail sync card: per-provider status, message count, and a
"Sync now" button. `mongoose release-check` also reports whether any provider is
authorized, so mail sync shows up in the readiness verdict.

## Notes

- Gmail/Microsoft are read-only (the OAuth scopes are *.readonly): Mongoose reads your
  mail to index it; it does not send or modify.
- `mail sync` and the background scheduler share one code path, there is no separate
  manual-vs-automatic sync to drift out of step.
