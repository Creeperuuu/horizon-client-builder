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
# selected mod's source anchors still resolve - becomes the build revision.
#
# When NO candidate plans the full set, the mods cannot be built together on
# one Horizon tree. The probe then keeps going instead of stopping at the
# first error: for every candidate it re-plans with the blocking mods excluded
# (attributing each failure to a mod via the error's mod_id, or - for
# mod-less errors like E_MODULE - to every mod whose [[module]] declares the
# missing destination file) and records a per-candidate compatibility matrix.
# The run summary ends with that matrix plus a suggested way to split the mods
# into groups that each build on their own revision, and names mods that fail
# everywhere (their own pin included) explicitly.
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
MATRIX=probe-matrix.json
# Extra `plan` runs the failure analysis may spend on attributing errors and
# probing compatible subsets (the happy path never touches this budget).
MATRIX_BUDGET=${MATRIX_PLAN_BUDGET:-40}
PLAN_RUNS=0
: > "$REPORT"
echo "[]" > "$MATRIX"
mkdir -p probe-logs

# Defensive: an upload branch with zero staged mods means a clean client
# build - nothing to probe (the workflow skips this step for that case too).
if ! ls mods/*/horizon-mixin.toml > /dev/null 2>&1; then
  echo "No staged mods - skipping the revision probe (clean client build)"
  exit 0
fi

mod_ids() { ls -d mods/*/ 2>/dev/null | sed 's|^mods/||; s|/$||' | sort; }

# Commit -> the mod(s) that pin it, for labeling candidates in the report.
OWNERS=$(python3 - "$FINAL_HORIZON_COMMIT" <<'PY'
import json, sys, tomllib
from pathlib import Path
owners = {}
for mf in sorted(Path("mods").glob("*/horizon-mixin.toml")):
    try:
        pin = tomllib.loads(mf.read_text(encoding="utf-8")).get(
            "compatibility", {}).get("horizon_revision")
    except Exception:
        pin = None
    if isinstance(pin, str):
        owners.setdefault(pin.lower(), []).append(mf.parent.name)
print(json.dumps(owners))
PY
)

candidate_label() { # $1 = sha -> only the human annotation (callers add the sha)
  python3 - "$1" "$OWNERS" "$FINAL_HORIZON_COMMIT" <<'PY'
import json, sys
sha, owners, requested = sys.argv[1], json.loads(sys.argv[2]), sys.argv[3]
pinned = owners.get(sha.lower(), [])
if sha == requested:
    print("(requested ref)", end="")
elif pinned:
    print(f"(pin of {', '.join(pinned)})", end="")
PY
}

# Parses a plan diagnostic JSON (pretty-printed on stderr) into PLAN_* globals.
parse_plan_log() { # $1 = log file
  PLAN_CODE=""; PLAN_MESSAGE=""; PLAN_MOD_ID=""; PLAN_TARGET=""; PLAN_FILE=""
  eval "$(python3 - "$1" <<'PY'
import json, shlex, sys
text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
data = {}
start = text.find("{")
if start != -1:
    try:
        parsed = json.loads(text[start:])
        data = {k: parsed[k] for k in ("code", "message", "mod_id", "target", "file")
                if isinstance(parsed.get(k), str)}
    except json.JSONDecodeError:
        pass
names = {"code": "PLAN_CODE", "message": "PLAN_MESSAGE", "mod_id": "PLAN_MOD_ID",
         "target": "PLAN_TARGET", "file": "PLAN_FILE"}
print(" ".join(f"{names[k]}={shlex.quote(v)}" for k, v in data.items()))
PY
)"
}

# Runs plan; on failure parses the diagnostic from stderr. Args: mods-dir label.
run_plan() {
  local mods_dir="$1" label="$2"
  local log="probe-logs/plan-$label.log"
  PLAN_RUNS=$((PLAN_RUNS + 1))
  if "$MIXINS_BIN" plan --workspace horizon --mods "$mods_dir" \
      --out "target/mixins/plan-$label.json" > "probe-logs/plan-$label.out" 2> "$log"; then
    return 0
  fi
  parse_plan_log "$log"
  return 1
}

# Mods whose [[module]] declares `file` = $1 (the E_MODULE diagnostic names
# the missing destination but not the mod; every mod that declares it blocks
# at this revision).
mods_declaring_module_file() {
  python3 - "$1" <<'PY'
import sys, tomllib
from pathlib import Path
dest = sys.argv[1]
for mf in sorted(Path("mods").glob("*/horizon-mixin.toml")):
    try:
        manifest = tomllib.loads(mf.read_text(encoding="utf-8"))
    except Exception:
        continue
    if any(m.get("file") == dest for m in manifest.get("module", []) if isinstance(m, dict)):
        print(mf.parent.name)
PY
}

# Attributes the current PLAN_* error to blocking mod id(s) (space separated,
# possibly several for a shared missing module destination). Extra plan runs
# count against the matrix budget. $1 = mods that may still be blamed.
attribute_error() { # $1 = remaining mod ids; sets LO_FULL_PASS=1 when its
                    # leave-one-out probe passed for every other remaining mod
  local remaining="$1"
  LO_FULL_PASS=""
  if [ -n "$PLAN_MOD_ID" ]; then
    printf '%s' "$PLAN_MOD_ID"
    return 0
  fi
  if [ "$PLAN_CODE" = "E_MODULE" ] && [ -n "$PLAN_FILE" ]; then
    local hits; hits=$(mods_declaring_module_file "$PLAN_FILE" | tr '\n' ' ')
    hits=${hits% }
    if [ -n "$hits" ]; then
      printf '%s' "$hits"
      return 0
    fi
  fi
  # Unknown mod-less error: leave-one-out over the remaining mods.
  local try
  for try in $remaining; do
    if [ "$PLAN_RUNS" -ge "$MATRIX_BUDGET" ]; then return 1; fi
    local rest=""
    for m in $remaining; do [ "$m" != "$try" ] && rest="$rest $m"; done
    rm -rf probe-mods; mkdir -p probe-mods
    local m
    for m in $rest; do cp -r "mods/$m" "probe-mods/$m"; done
    if run_plan probe-mods "leaveout-$try"; then
      printf '%s' "$try"
      CONFIRMED_EXTRA="$rest"
      LO_FULL_PASS=1
      return 0
    fi
    if [ -n "$PLAN_MOD_ID" ]; then
      printf '%s' "$PLAN_MOD_ID"
      return 0
    fi
  done
  return 1
}

short_error() {
  local detail="${PLAN_TARGET:-$PLAN_FILE}"
  if [ -n "$detail" ]; then
    printf '%s (%s)' "$PLAN_CODE" "$detail"
  else
    printf '%s' "${PLAN_CODE:-unknown error}"
  fi
}

# Failure-path analysis for one candidate: exclude blocking mods one at a
# time until the rest plans cleanly, recording who blocks and why. Seeds from
# the full-set plan log the main loop just produced. $1 = sha, $2 = its log,
# $3 = human label for the candidate.
analyze_candidate() { # $1 = sha, $2 = full-set plan log, $3 = label
  local sha="$1"
  local confirmed="" bad_mods="" partial="" CONFIRMED_EXTRA="" LO_FULL_PASS=""
  local remaining; remaining=$(mod_ids)
  parse_plan_log "$2"
  # Seed: attribute the full-set error without re-running the same plan.
  if [ -n "${PLAN_CODE:-}" ]; then
    local seeded; seeded=$(attribute_error "$remaining") && true
    if [ -n "${seeded:-}" ]; then
      local first; first=$(printf '%s' "$seeded" | awk '{print $1}')
      local before=""
      local m
      for m in $remaining; do
        [ "$(printf '%s\n' "$m" "$first" | sort | head -1)" = "$m" ] \
          && [ "$m" != "$first" ] && before="$before $m"
      done
      confirmed="$before$CONFIRMED_EXTRA"
      bad_mods="$seeded"
      if [ "${LO_FULL_PASS:-}" = "1" ]; then
        remaining=""
      else
        local next="" skip b
        for m in $remaining; do
          skip=""
          for b in $seeded; do [ "$m" = "$b" ] && skip=1; done
          [ -z "$skip" ] && next="$next $m"
        done
        remaining=${next# }
      fi
      {
        echo "BLOCKS $sha: $seeded <- $(short_error)"
      } >> "$REPORT"
    fi
  fi
  local attempt=0
  while [ -n "$remaining" ]; do
    if [ "$PLAN_RUNS" -ge "$MATRIX_BUDGET" ]; then partial=1; break; fi
    attempt=$((attempt + 1))
    rm -rf probe-mods
    mkdir -p probe-mods
    local m
    for m in $remaining; do cp -r "mods/$m" "probe-mods/$m"; done
    if run_plan probe-mods "matrix-$sha-$attempt"; then
      confirmed="$confirmed $remaining"
      break
    fi
    CONFIRMED_EXTRA=""
    local culprit
    if ! culprit=$(attribute_error "$remaining") || [ -z "$culprit" ]; then
      partial=1
      {
        echo "PARTIAL $sha: could not attribute a blocking error:"
        echo "  ${PLAN_CODE:-unknown}: ${PLAN_MESSAGE:-no diagnostic parsed}"
      } >> "$REPORT"
      break
    fi
    local first; first=$(printf '%s' "$culprit" | awk '{print $1}')
    local before=""
    for m in $remaining; do
      [ "$(printf '%s\n' "$m" "$first" | sort | head -1)" = "$m" ] \
        && [ "$m" != "$first" ] && before="$before $m"
    done
    confirmed="$confirmed $before$CONFIRMED_EXTRA"
    bad_mods="$bad_mods $culprit"
    {
      echo "BLOCKS $sha: $culprit <- $(short_error)"
    } >> "$REPORT"
    if [ "${LO_FULL_PASS:-}" = "1" ]; then
      remaining=""
      break
    fi
    local next="" skip b
    for m in $remaining; do
      skip=""
      for b in $culprit; do [ "$m" = "$b" ] && skip=1; done
      [ -z "$skip" ] && next="$next $m"
    done
    remaining=${next# }
  done
  python3 - "$sha" "$confirmed" "$bad_mods" "${partial:-}" "${3:-}" <<'PY'
import json, sys
sha, partial, label = sys.argv[1], sys.argv[4] == "1", sys.argv[5]
good = sorted(set(sys.argv[2].split()))
bad = sorted(set(sys.argv[3].split()))
rows = json.load(open("probe-matrix.json"))
rows.append({"sha": sha, "label": label, "good": good, "bad": bad,
             "partial": partial})
json.dump(rows, open("probe-matrix.json", "w"))
PY
}

CANDIDATES=""
if [ -n "${PINS_JSON:-}" ] && printf '%s' "$PINS_JSON" | grep -q '"pins":\[[^]]'; then
  # Order the mod pins by commit date, newest first: a mod is most likely to
  # keep working on commits at or after the one it was written for.
  CANDIDATES=$(printf '%s' "$PINS_JSON" | python3 -c '
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
FAILED_SHAS=""
for CANDIDATE in $CANDIDATES; do
  echo "::group::Probing Horizon revision $CANDIDATE"
  if ! git -C horizon checkout --force --detach "$CANDIDATE" > /dev/null 2>&1; then
    echo "SKIP $CANDIDATE $(candidate_label "$CANDIDATE"): not reachable in the Horizon clone" >> "$REPORT"
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
    grep -E '"code"|"target"|"mod_id"|"file"|"message"' "probe-logs/plan-$SHA.log" | head -30
  } >> "$REPORT"
  FAILED_SHAS="$FAILED_SHAS $SHA"
  echo "::endgroup::"
done

if [ -z "$WINNER" ]; then
  # No candidate plans the full set. Work out - per candidate - which mods
  # block and which subset would plan, so the report can suggest a split.
  if [ "$(mod_ids | wc -l)" -gt 1 ]; then
    for SHA in $FAILED_SHAS; do
      echo "::group::Compatibility analysis at $SHA"
      git -C horizon checkout --force --detach "$SHA" > /dev/null 2>&1
      bash scripts/materialize-symlinks.sh horizon > /dev/null
      python scripts/prepare_mods.py --rewrite-staged --output-dir mods \
        --force-revision "$SHA" > /dev/null
      rm -rf target/mixins
      mkdir -p target/mixins
      analyze_candidate "$SHA" "probe-logs/plan-$SHA.log" "$(candidate_label "$SHA")"
      echo "::endgroup::"
    done
  fi
  python3 - <<'PY' > probe-failure-report.md
import json

rows = json.load(open("probe-matrix.json"))
all_mods = sorted({m for r in rows for m in r["good"] + r["bad"]})
lines = []
lines.append("## No Horizon revision satisfies all selected mods together")
lines.append("")
lines.append("The probe tried every mod's pinned commit (newest first), then the requested")
lines.append("ref. No single revision plans all of them, so they cannot be built into one")
lines.append("client as-is. Compatibility per candidate (`✔` = plans cleanly there):")
lines.append("")
lines.append("| Horizon revision | ✔ plans | ✘ blocks |")
lines.append("|---|---|---|")
for r in rows:
    label = r.get("label") or ""
    short = f"`{r['sha'][:12]}`" + (f" {label}" if label else "")
    good = ", ".join(r["good"]) or "—"
    bad = ", ".join(r["bad"]) or "—"
    if r.get("partial"):
        bad += " (analysis incomplete)"
    lines.append(f"| {short} | {good} | {bad} |")
remaining = set(all_mods)
groups = []
for r in rows:
    if not remaining:
        break
    take = set(r["good"]) & remaining
    if not take:
        continue
    groups.append((take, r["sha"]))
    remaining -= take
if groups:
    lines.append("")
    lines.append("**Suggested split** - one build per group; the probe re-verifies")
    lines.append("each group and lands it on the listed revision:")
    lines.append("")
    labels = {r["sha"]: r.get("label") or "" for r in rows}
    for i, (mods, sha) in enumerate(groups, 1):
        label = labels.get(sha, "")
        lines.append(f"{i}. {', '.join(sorted(mods))} → Horizon `{sha[:12]}`" + (f" {label}" if label else ""))
    if remaining:
        lines.append("")
        lines.append(f"No candidate plans {', '.join(sorted(remaining))}: these mods fail")
        lines.append("even at their own pin and need a fix (stale anchor or pin) first.")
else:
    lines.append("")
    lines.append("Every selected mod fails at every candidate; the mods (or their pins)")
    lines.append("need fixing - see the per-candidate errors below.")
lines.append("")
lines.append("Blocking errors per candidate:")
lines.append("")
lines.append("```")
print("\n".join(lines))
for line in open("probe-report.txt", encoding="utf-8"):
    if line.startswith(("FAIL", "BLOCKS", "PARTIAL", "SKIP")):
        print(line.rstrip())
print("```")
PY
  {
    cat probe-failure-report.md
    echo
    echo "Each mod pin points at the commit its mod was written for; when none of them"
    echo "plans cleanly together, the mods were written for incompatible Horizon"
    echo "versions. Build the suggested groups in separate runs, or update the mods"
    echo "named above to versions written for one common Horizon revision."
  } >> "$GITHUB_STEP_SUMMARY"
  cat probe-failure-report.md
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
