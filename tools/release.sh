#!/usr/bin/env bash
# Release EZ Maintenance++: build the deterministic zip, tag it, and publish
# the zip as a GitHub Release asset on THIS repo (moquette/kodi-ezmpp).
#
# Release discipline:
# build -> CI GATE -> sha256 -> tag -> gh release create -> verify the asset is
# really, anonymously downloadable and the bytes match what was built. A release
# that fails the verification step is a release that would 404 or ship the wrong
# bytes to a live box, so this script treats verification as mandatory, not
# optional.
#
# The CI GATE (see its block below) is why this script can no longer outrun the
# repo's own test suite. It refuses to tag anything unless the `test` job of
# .github/workflows/ci.yml has already reported completed/success for the exact
# commit being tagged. Added 2026-07-21 after this script released two builds
# from red commits straight onto the live fleet catalog.
#
# Usage:
#   tools/release.sh              # gate + tag + release + verify
#   tools/release.sh --dry-run    # build + show the plan, create nothing
#                                 # (offline: does NOT run the CI gate)
set -euo pipefail
cd "$(dirname "$0")/.."

ADDON=script.ezmaintenanceplusplus
REPO="moquette/kodi-ezmpp"
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

./build.sh

VERSION=$(grep '<addon ' "$ADDON/addon.xml" | grep -oE 'version="[^"]+"' | head -1 | sed 's/version="//; s/"//')
ZIP="dist/${ADDON}-${VERSION}.zip"
SHA256=$(shasum -a 256 "$ZIP" | awk '{print $1}')
TAG="v${VERSION}"
ASSET_URL="https://github.com/${REPO}/releases/download/${TAG}/${ADDON}-${VERSION}.zip"

echo
echo "version:    ${VERSION}"
echo "zip:        ${ZIP}"
echo "sha256:     ${SHA256}"
echo "tag:        ${TAG}"
echo "asset url:  ${ASSET_URL}"

if [ "$DRY_RUN" = "1" ]; then
  echo
  echo "(--dry-run: nothing tagged or released)"
  exit 0
fi

if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  echo
  echo "FATAL: release ${TAG} already exists on ${REPO}." >&2
  echo "  Bump addon.xml's version before releasing again, or delete the" >&2
  echo "  existing release first if this is a genuine re-cut." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# CI GATE. A release may not be cut from a commit whose CI has not gone green.
#
# 2026-07-21 incident: v2026.07.21.3 (94d6747) and v2026.07.21.4 (0545c9f) were
# both released by this script from commits whose `test` job was RED on
# tests/test_storage_change_requires_device_verification.py, and both reached
# the live hub catalog within a minute. CI's own publish job CANNOT do this -
# .github/workflows/ci.yml:87 declares `needs: test`, and the publish job was
# `skipped` in both runs (29853877919, 29856967414). This script did it,
# because it went straight from build.sh to `gh release create` and never asked
# anything about the commit it was tagging. CLAUDE.md:111 already said "Tests
# are mandatory before any release"; prose did not hold, so this is the
# mechanical version.
#
# Fails CLOSED on every non-green state, not only on red: missing run, queued,
# in-progress, cancelled, or an unreachable API all block. That matters because
# on 2026-07-21 the release was created ELEVEN SECONDS BEFORE the CI run for
# its own commit started (release createdAt 17:39:24Z, run createdAt
# 17:39:35Z) - a "not red" test would have passed that race. Only "the `test`
# check for this exact sha is completed AND success" opens the gate.
#
# HEAD must also equal origin/main. build.sh zips the WORKING TREE while
# `gh release create --target main` tags whatever origin/main resolves to, and
# the sha256 verification below compares the upload against the local build,
# never against the tagged source - so a dirty or behind tree ships bytes that
# no tagged commit contains, and nothing downstream can detect it.
git fetch --quiet origin main
SHA=$(git rev-parse origin/main)

if [ "$(git rev-parse HEAD)" != "$SHA" ]; then
  echo >&2
  echo "FATAL: HEAD is not origin/main." >&2
  echo "  HEAD        $(git rev-parse HEAD)" >&2
  echo "  origin/main ${SHA}" >&2
  echo "  --target main would tag origin/main while the zip above was built" >&2
  echo "  from this working tree. Push (or reset) first, then re-run." >&2
  exit 1
fi

if [ -n "$(git status --porcelain -- "$ADDON")" ]; then
  echo >&2
  echo "FATAL: uncommitted changes under ${ADDON}/." >&2
  git status --porcelain -- "$ADDON" >&2
  echo "  The zip would carry bytes that the tagged commit does not." >&2
  exit 1
fi

CI_STATE=$(gh api "repos/${REPO}/commits/${SHA}/check-runs" \
  --jq '[.check_runs[] | select(.name=="test")]
        | if length == 0 then "missing"
          else (.[0] | .status + "/" + (.conclusion // "null")) end' 2>/dev/null) ||
  CI_STATE="unavailable"

if [ "$CI_STATE" != "completed/success" ]; then
  echo >&2
  echo "FATAL: CI is not green for ${SHA}." >&2
  echo "  workflow: .github/workflows/ci.yml, job 'test'" >&2
  echo "  state:    ${CI_STATE}   (required: completed/success)" >&2
  echo >&2
  echo "  A red or unfinished gate has four legal responses, and releasing" >&2
  echo "  anyway is not one of them: fix the code, produce the evidence the" >&2
  echo "  gate asked for, report the red verbatim, or ask the owner." >&2
  echo "  Never edit the gate, deselect the test, or waive it." >&2
  echo >&2
  echo "  See it:  gh run list --repo ${REPO} --commit ${SHA}" >&2
  echo "  Watch:   gh run watch --repo ${REPO} \\" >&2
  echo "             \$(gh run list --repo ${REPO} --commit ${SHA} --limit 1 \\" >&2
  echo "                 --json databaseId --jq '.[0].databaseId')" >&2
  echo >&2
  echo "  If the state is 'unavailable', this gate could not reach the GitHub" >&2
  echo "  API. That is still a block: an unverifiable gate is not a green one." >&2
  exit 1
fi

echo
echo "CI gate: ${SHA} is completed/success on job 'test' - clear to release."

# --target "$SHA" (NOT a local tag, NOT the branch name): the tag must resolve
# against a reviewed, pushed commit, never an unpushed local one. `git tag -f`
# would tag local HEAD, and gh would then push THAT tag (and everything it
# points to - i.e. whatever you have locally, committed or not) to make it
# resolvable remotely.
#
# CORRECTED 2026-07-21: this said `--target main` and that was subtly wrong.
# The branch name is resolved by GitHub when the request lands, so the commit
# actually tagged is whatever main points at THEN - not the commit the gate
# above just proved green. Any push between the two tags an ungated commit,
# and the sha256 check below cannot see it (it compares the upload to the
# local build, never to the tagged source). $SHA is origin/main as read by
# the gate, so it is still by definition pushed and reviewable; it just
# cannot drift. This matches what CI does: .github/workflows/ci.yml:126 uses
# --target "${GITHUB_SHA}".
gh release create "$TAG" "$ZIP" \
  --repo "$REPO" \
  --target "$SHA" \
  --title "EZ Maintenance++ ${VERSION}" \
  --notes "sha256: ${SHA256}"

echo
echo "verifying the release asset is live and byte-correct..."
TMP=$(mktemp -t ezm_verify.XXXXXX).zip
curl -sSfL "$ASSET_URL" -o "$TMP"
DOWNLOADED_SHA=$(shasum -a 256 "$TMP" | awk '{print $1}')
rm -f "$TMP"

if [ "$DOWNLOADED_SHA" != "$SHA256" ]; then
  echo "FATAL: downloaded asset sha256 mismatch (expected ${SHA256}, got ${DOWNLOADED_SHA})" >&2
  echo "  The release exists but does NOT match the build - do not point the" >&2
  echo "  proxy's repository.json at this tag until this is resolved." >&2
  exit 1
fi

echo "OK - ${ASSET_URL}"
echo "OK - anonymous download sha256-verified against the local build"
