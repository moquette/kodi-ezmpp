#!/usr/bin/env bash
# Build the installable Kodi zip for EZ Maintenance++.
# Output: dist/script.ezmaintenanceplusplus-<version>.zip (top-level folder = the add-on id).
#
# Thin wrapper over tools/build.py, which builds the zip DETERMINISTICALLY
# (sorted members, fixed 1980-01-01 timestamps) - same discipline as
# tony7bones.github.io's generate_repo.py, so a
# rebuild of the same source is byte-for-byte identical and the sha256 in a
# release's notes actually means something.
set -euo pipefail
cd "$(dirname "$0")"

# Note: the Dropbox sign-in QR is generated ON THE DEVICE at sign-in time (PKCE makes
# the authorize URL change every sign-in, so it can't be pre-baked). The vendored
# encoder lives at resources/lib/modules/_vendor/qrcode + _qrgen.py - no build step.

PYTHON="${PYTHON:-python3}"
command -v /opt/homebrew/bin/python3 >/dev/null 2>&1 && PYTHON=/opt/homebrew/bin/python3

exec "$PYTHON" tools/build.py "$@"
