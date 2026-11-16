#!/usr/bin/env bash
# Hydrate this (sterile) Mongoose install with the REAL RTM config from the private config repo.
#
# The code ships fictional RTM identity (director, addresses, phone, bank, register). This script
# imports the real canonical config from a private source into the install's data_dir, where it
# transparently overrides the packaged fictional assets. No code change or rebuild is needed.
#
# Usage:
#   scripts/rtm_hydrate.sh <path-to-config-repo>      # from a local checkout of the private repo
#   scripts/rtm_hydrate.sh <git-url>                  # clones the private repo, then imports
#
# The config source must contain: rtm/property.json and rtm_templates/docx/*.docx
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${1:-}"
if [ -z "$SRC" ]; then
  echo "usage: scripts/rtm_hydrate.sh <path-to-config-repo | git-url>" >&2
  exit 2
fi

# If SRC looks like a git URL, clone it to a temp dir first.
TMP=""
if printf '%s' "$SRC" | grep -qE '^(https?|git|ssh)://|^git@'; then
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  echo "cloning private config: $SRC"
  git clone --depth 1 "$SRC" "$TMP" >/dev/null
  SRC="$TMP"
fi
SRC="$(cd "$SRC" && pwd)"

# The source must actually carry the real config, not be an empty/wrong checkout.
for req in rtm/property.json rtm_templates/docx; do
  [ -e "$SRC/$req" ] || { echo "ERROR: config source missing $req (is this the private config repo?)" >&2; exit 1; }
done

# Resolve data_dir exactly as the app does: MONGOOSE_DB_PATH's parent, else ~/.mongoose/data.
if [ -n "${MONGOOSE_DB_PATH:-}" ]; then
  DATA_DIR="$(cd "$(dirname "$MONGOOSE_DB_PATH")" && pwd)"
else
  DATA_DIR="$HOME/.mongoose/data"
fi
echo "target data_dir: $DATA_DIR"
mkdir -p "$DATA_DIR/rtm" "$DATA_DIR/rtm_templates/docx"

# Back up anything we are about to overwrite, so a bad import is reversible.
STAMP="$(date +%Y%m%d-%H%M%S 2>/dev/null || echo prev)"
for rel in rtm/property.json rtm_templates/docx; do
  tgt="$DATA_DIR/$rel"
  if [ -e "$tgt" ]; then
    bak="$DATA_DIR/.rtm-config-backup-$STAMP/$rel"
    mkdir -p "$(dirname "$bak")"; cp -R "$tgt" "$bak"
  fi
done

cp "$SRC/rtm/property.json" "$DATA_DIR/rtm/property.json"
cp "$SRC"/rtm_templates/docx/*.docx "$DATA_DIR/rtm_templates/docx/"
echo "copied real config into data_dir."

# Verify: the company block must validate (fail loud, never a silent placeholder). Uses the venv
# if present so company_profile()'s strict check runs against the just-imported config.
PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY="python3"
MONGOOSE_DB_PATH="${MONGOOSE_DB_PATH:-$DATA_DIR/mongoose.sqlite3}" \
PYTHONPATH="$ROOT/src" "$PY" - <<'PYCODE'
from mongoose.services.rtm_example_court import company_profile
p = company_profile()  # raises if the company block is absent/incomplete
assert p.get("blocks") and p.get("bank", {}).get("sort_code"), "company block loaded but incomplete"
print("  verified: company identity validates (director/address/phone/bank present).")
PYCODE
echo "hydrate complete. Restart Mongoose (launchctl kickstart) so the running server reloads config."
