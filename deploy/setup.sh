#!/usr/bin/env bash
# Unprivileged setup. Everything here runs as your normal user; the steps that
# genuinely need root are listed separately in deploy/ROOT_STEPS.md so you can
# read them before running them.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS_DIR="${WAYFARE_SECRETS_DIR:-$REPO_DIR/secrets}"

cd "$REPO_DIR"

echo "==> Python environment"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -e .

echo "==> Airport database"
./.venv/bin/wayfare fetch-airports

echo "==> Tokens"
mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"
for name in owner_token agent_token; do
  if [[ -f "$SECRETS_DIR/$name" ]]; then
    echo "    $name already exists, keeping it"
  else
    ./.venv/bin/wayfare token > "$SECRETS_DIR/$name"
    chmod 600 "$SECRETS_DIR/$name"
    echo "    generated $name"
  fi
done

echo
echo "==> Component check"
./.venv/bin/wayfare doctor || true

cat <<EOF

Next steps
----------
1. OCR and barcode decoding need system packages. See deploy/ROOT_STEPS.md.

2. Google Calendar access: start the server and open /setup in a browser.
   That page walks through it and takes the JSON as an upload, so you do not
   need to know where this directory is. Terminal alternative:
     ./.venv/bin/wayfare auth   (needs a Desktop-type OAuth client)

3. Model backend (for screenshots with no barcode): also on the /setup page.
   It links to OpenRouter, takes the key, and tests it before saying it works.

4. Your tokens (keep these out of chat logs and out of the repo):
     owner token: $SECRETS_DIR/owner_token
     agent token: $SECRETS_DIR/agent_token

5. Run it:
     ./.venv/bin/wayfare serve --port 8791
   or install the user service, see deploy/ROOT_STEPS.md.
EOF
