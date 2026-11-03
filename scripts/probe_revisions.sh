#!/usr/bin/env bash
# The mixed-revision .hnmod workaround.
#
# Every .hnmod pins the Horizon commit it was written against in
# [compatibility].horizon_revision, and the mixin tooling (a) refuses to run
# against a different commit (E_REVISION) and (b) fails when a mod's injected
# source anchors, like horizon_protocol::frame::decompress, no longer exist at
# the checked-out commit (E_TARGET_NOT_FOUND). Rewriting the pins - the old
# "override" workaround - silences (a) but cannot fix (b), which is what
# actually broke builds of mods written for older Horizon commits.
#
# This script instead PROBES candidate revisions: every distinct mod pin,
# newest commit first, then the requested commit as a fallback. For each
# candidate it checks out the Horizon worktree at that commit, neutralizes the
# mods' pins (so E_REVISION cannot mask the anchor errors being probed for)
# and runs `plan`. The first candidate whose plan succeeds - i.e. where every
# selected mod's anchors still resolve - becomes the build revision. If no
# candidate works, a per-candidate report is written to the run summary and
# the build fails, so genuinely incompatible mods fail loudly instead of
# silently.
#
# Required environment:
#   FINAL_HORIZON_COMMIT  commit the Horizon worktree is currently at (the
#                         requested ref, already checked out by the workflow)
#   MIXINS_BIN            path to the built horizon-mixins CLI binary
#   PINS_JSON             output of `prepare_mods.py --list-pins`, or empty
#   GITHUB_OUTPUT, GITHUB_ENV, GITHUB_STEP_SUMMARY   (set by Actions)
#
# Outputs: GITHUB_OUTPUT revision=<winning commit>, GITHUB_ENV
# FINAL_HORIZON_COMMIT + PROBE_REPORT. The Horizon worktree is left at the
# winner with every mod pin rewritten to it, ready for `apply`.
set -uo pipefail

: "${FINAL_HORIZON_COMMIT:?FINAL_HORIZON_COMMIT is required}"
: "${MIXINS_BIN:?MIXINS_BIN is required}"
: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"
: "${GITHUB_ENV:?GITHUB_ENV is required}"
: "${GITHUB_STEP_SUMMARY:?GITHUB_STEP_SUMMARY is required}"

REPORT=probe-report.txt
: > "$REPORT"
mkdir -p probe-logs

# Defensive: an upload branch with zero staged mods means a clean client
# build - nothing to probe (the workflow skips this step for that case too).
if ! ls mods/*/horizon-mixin.toml > /dev/null 2>&1; then
  echo "No staged mods - skipping the revision probe (clean client build)"
  exit 0
fi

CANDIDATES=""
if [ -n "${PINS_JSON:-}" ] && printf '%s' "$PINS_JSON" | grep -q '"pins":\[[^]]'; then
  # Order the mod pins by commit date, newest first: a mod is most likely to
  # keep working on commits at or after the one it was written for.
  CANDIDATES=$(printf '%s' "$PINS_JSON" | python -c '
import json, subprocess, sys
pins = sorted(set(json.load(sys.stdin).get("pins", [])))
for pin in pins:
    result = subprocess.run(
        ["git", "-C", "horizon", "rev-list", "-n", "1", "--timestamp", pin],
        capture_output=True, text=True)
    stamp = result.stdout.strip().split(" ")[0] if result.returncode == 0 else ""
    print(stamp if stamp.isdigit() else "0", pin)
' | sort -k1,1rn -k2,2 | awk '{print $2}')
fi
# The requested commit is the last-resort candidate; with no pins it is the
# only one.
CANDIDATES="$CANDIDATES $FINAL_HORIZON_COMMIT"
# Deduplicate, keeping the probe order.
CANDIDATES=$(printf '%s\n' $CANDIDATES | awk '!seen[$0]++')
echo "Probing Horizon revisions in order: $(echo $CANDIDATES | tr '\n' ' ')"

WINNER=""
for CANDIDATE in $CANDIDATES; do
  echo "::group::Probing Horizon revision $CANDIDATE"
  if ! git -C horizon checkout --force --detach "$CANDIDATE" > /dev/null 2>&1; then
    echo "SKIP $CANDIDATE: not reachable in the Horizon clone" >> "$REPORT"
    echo "::endgroup::"
    continue
  fi
  SHA=$(git -C horizon rev-parse HEAD)
  if ! bash scripts/materialize-symlinks.sh horizon; then
    echo "FAIL $SHA: could not materialize tracked symlinks" >> "$REPORT"
    echo "::endgroup::"
    continue
  fi
  # Neutralize every mod pin for this attempt so E_REVISION cannot mask the
  # anchor errors the probe is actually looking for.
  if ! python scripts/prepare_mods.py --rewrite-staged --output-dir mods \
      --force-revision "$SHA" > /dev/null; then
    echo "FAIL $SHA: could not rewrite the mod pins" >> "$REPORT"
    echo "::endgroup::"
    continue
  fi
  # The mixin CLI refuses to replace an existing stage (E_STAGE_OWNERSHIP).
  rm -rf target/mixins
  mkdir -p target/mixins
  if "$MIXINS_BIN" plan --workspace horizon --mods mods \
      --out target/mixins/plan.json > "probe-logs/plan-$SHA.log" 2>&1; then
    WINNER="$SHA"
    echo "WIN $SHA: all selected mods' source anchors resolve here" >> "$REPORT"
    echo "::endgroup::"
    break
  fi
  {
    echo "FAIL $SHA:"
    grep -E '"code"|"target"|"mod_id"|"message"' "probe-logs/plan-$SHA.log" | head -30
  } >> "$REPORT"
  echo "::endgroup::"
done

if [ -z "$WINNER" ]; then
  {
    echo "## No Horizon revision satisfies all selected mods"
    echo
    echo "Probed candidates (mod pins newest first, then the requested ref):"
    echo
    echo '```'
    cat "$REPORT"
    echo '```'
    echo
    echo "Each mod pin points at the commit its mod was written for; when none of them"
    echo "plans cleanly together, the mods were written for incompatible Horizon"
    echo "versions. Try building the conflicting mods in separate runs, or update them"
    echo "to versions written for one common Horizon revision."
  } >> "$GITHUB_STEP_SUMMARY"
  cat "$REPORT"
  echo "No candidate Horizon revision plans all selected mods cleanly; see the probe report above." >&2
  exit 1
fi

echo "revision=$WINNER" >> "$GITHUB_OUTPUT"
{
  echo "FINAL_HORIZON_COMMIT=$WINNER"
  echo "PROBE_REPORT<<PROBE_REPORT_EOF"
  cat "$REPORT"
  echo "PROBE_REPORT_EOF"
} >> "$GITHUB_ENV"
echo "All selected mods plan cleanly at Horizon $WINNER"
