# Finding files everywhere: cross-device files + email attachments

Two indexes let Mongoose answer "where is that file?" no matter where it lives:

1. **Files across your devices** (MacBook, Android phone), tagged by device.
2. **Every email attachment you've received**, findable by filename, sender, or
   subject, with a link back to the email it came in.

## 1. Cross-device files (MacBook + Android via Syncthing)

Mongoose indexes folders on the Mac. To make your phone's files searchable too,
the local-first approach is to **sync a phone folder to the Mac peer-to-peer**,
then index the synced folder with a device label. No phone app to build, no push
API, no cloud.

### Which sync app (2026)

Use **Syncthing-Fork** (`com.github.catfriend1.syncthingfork`), from F-Droid. The
ORIGINAL Syncthing Android app was discontinued in December 2024; the actively
maintained fork (researchxxl/Catfriend1, v2.1.1.0, mid-2026) is the one to install.
It is open-source, peer-to-peer, end-to-end encrypted, and no third-party cloud.
Resilio Sync is a proprietary fallback; Round Sync or FolderSync (to a self-hosted
SMB/SFTP share) work for scheduled push.

Two things make or break it:
- **Grant "All files access"** (Settings -> Special app access -> All files access)
  so it can see your whole shared storage, not just media folders.
- **Exclude it from battery optimization**, or the OS silently kills background
  sync (the single most common "it stopped working" cause).

The honest ceiling: a non-rooted phone exposes everything under
`/storage/emulated/0` EXCEPT `Android/data/` and `Android/obb/` (app-private
storage). That captures essentially all your documents, downloads, photos and
media; it cannot reach files locked inside other apps without rooting.

### Setup

1. F-Droid -> Syncthing-Fork on the phone; All-files access; exclude from battery
   optimization. Install Syncthing on the Mac (`brew install syncthing`), run it
   as an always-on service.
2. Pair the devices. On the phone, add a folder for `/storage/emulated/0` (enter
   the path directly in the web GUI; the file picker greys out the root). Set the
   phone side **Send Only** and the Mac side **Receive Only**, and enable
   **versioning** on the Mac side (Syncthing is sync, not backup: this protects
   against a deletion propagating).
3. Index each device's files with a `--device` tag:
   ```
   mongoose index-files ~/Documents --device macbook
   mongoose index-files ~/SyncPhone --device phone
   ```
4. Search across both at once:
   ```
   mongoose search-files "tax return"
   ```
   Results show which device each file is on:
   ```
   [macbook] tax_return_2025.md  (/Users/you/Documents/tax_return_2025.md)
   [phone]   taxes_scan.pdf      (/Users/you/SyncPhone/Downloads/taxes_scan.pdf)
   ```

Re-run `index-files` to refresh; it only re-reads changed files (compares mtime).
A cron/launchd job keeps it current.

### Searching INSIDE files (content), not just names

The indexer reads plain-text types directly, and extracts text from **PDF and Word
(.docx)** so you can search by what's written inside them, not just the filename.
This needs two optional libraries on the Mac:
```
pip install pypdfium2 python-docx
```
`pypdfium2` is Apache/BSD-licensed (deliberately not the AGPL PyMuPDF). If these
aren't installed, the indexer still records every filename, you just don't get
content search for PDFs/Word until they are.

Scanned PDFs and images need OCR, which is a deliberate seam: on macOS, Apple's
Vision OCR (via `ocrmac`/`ocrit`) is the recommended local engine. The extractor
has an `ocr_image` hook a macOS caller can wire up; it's off by default because
OCR is platform-specific. Until wired, a scanned PDF is findable by filename but
not by its text.

## 2. Email attachments

When Mongoose syncs email (IMAP today; Gmail/Graph connectors exist), it now
records every attachment's metadata: filename, type, size, the sender, subject,
date, and which email it came in. Find any attachment you've ever received:

```
mongoose search-attachments "bundle"
mongoose search-attachments "service charges"   # matches the email subject too
mongoose search-attachments "lawfirm"          # matches the sender
```

Output names the file, who sent it, the email it's in, and the date, so you can
open the original message. Private (high/secret) attachments are flagged
`[private]` so they aren't surfaced carelessly.

Trigger an email sync (which now also indexes attachments):
```
mongoose sync-imap --mailbox INBOX --limit 200
```

### Content search

Attachment search covers **filename, sender, subject, AND the text inside PDF and
Word attachments** (with `pypdfium2` + `python-docx` installed, as above). So a
phrase buried in a PDF bundle is findable:
```
mongoose search-attachments "standing charge"   # matches text inside a PDF
```
Scanned-image PDFs need OCR (the same Apple Vision seam as for files); until that's
wired they're findable by filename. The `content_indexed` count in
`mongoose doctor` shows how many attachments have their contents indexed.

## Both surfaces are available to Claude too

Through the local MCP server, Claude Desktop/Code can call `search_files` (shows
device) and `search_attachments`, so you can ask Claude "find the hearing bundle
attachment" and it searches your local index, nothing leaves the machine.
