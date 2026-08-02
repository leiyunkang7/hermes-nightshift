#!/usr/bin/env bash
# dogfood.sh — capture one real /nightshift run for issue #4 acceptance.
#
# Usage:
#   scripts/dogfood.sh <profile> <run-num> "<goal>"
#   scripts/dogfood.sh research 1 "Survey 5 AI agent business models worth pursuing in 2026"
#   scripts/dogfood.sh code-review 2 "Review the README in my nightshift repo"
#   scripts/dogfood.sh synthesis 3 "Draft a weekly progress summary from /tmp/notes/"
#
# What it does:
#   1. Starts the nightshift background task via /nightshift (real plugin path)
#   2. Sleeps PAUSE_DELAY seconds (default 30), then issues /nightshift-pause
#   3. Stages an injection via /nightshift-inject (you can pass INJECT_TEXT env)
#   4. Issues /nightshift-resume
#   5. Sleeps COMPLETE_DELAY seconds (default 60), then issues /nightshift-status
#   6. Copies the live run dir from ~/.hermes/nightshift/runs/<tid>/ into
#      ./docs/dogfood/<profile>-<n>/<task_id>/  and writes a session.md
#      with timing data and the captured status string.
#
# Why bash:
#   Each step calls `hermes chat -q` so the plugin is loaded for real.
#   Python would re-implement the plugin loader — no upside for dogfood.
#
# Config (env, optional):
#   PAUSE_DELAY      seconds to wait before pause         (default 30)
#   COMPLETE_DELAY   seconds to wait before final status  (default 60)
#   INJECT_TEXT      body for the staged injection         (default "continue,
#                    but watch for the second of three items in the source")
#   HERMES_HOME      override ~/.hermes (test isolation)   (default $HOME/.hermes)
#   DRY_RUN=1        print commands instead of running     (default 0)

set -euo pipefail

if [[ $# -lt 3 ]]; then
    echo "usage: $0 <profile> <run-num> \"<goal>\"" >&2
    echo "  profile in {research, code-review, synthesis}" >&2
    echo "  run-num is an integer 1..3 (issue #4 asks for 3 real runs)" >&2
    exit 2
fi

PROFILE="$1"
RUN_NUM="$2"
shift 2
GOAL="$*"

case "$PROFILE" in
    research|code-review|synthesis) ;;
    *) echo "profile must be one of: research code-review synthesis" >&2; exit 2 ;;
esac

PAUSE_DELAY="${PAUSE_DELAY:-30}"
COMPLETE_DELAY="${COMPLETE_DELAY:-60}"
INJECT_TEXT="${INJECT_TEXT:-continue, but watch for the second of three items in the source}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

# Resolve repo root to the directory containing this script.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
DEST="$REPO_ROOT/docs/dogfood/$PROFILE-$RUN_NUM"
mkdir -p "$DEST"

run_step() {
    # run_step <label> <single-shot hermes query>
    local label="$1"; shift
    local query="$1"; shift
    echo
    echo "==> $label"
    echo "    query: $query"
    if [[ "${DRY_RUN:-0}" = "1" ]]; then
        echo "    (dry-run; hermes not invoked)"
        return 0
    fi
    local out
    out="$(HERMES_HOME="$HERMES_HOME" hermes chat -q "$query" 2>&1)" || true
    echo "$out"
    printf '%s' "$out"
}

# Step 1: dispatch
START_TS="$(date -u +%s)"
RUN_OUTPUT="$(run_step "dispatch /nightshift \"$GOAL\"" "/nightshift \"$GOAL\"")"

# Extract the task_id (first line: "nightshift: dispatched <task_id>")
TASK_ID="$(printf '%s\n' "$RUN_OUTPUT" | sed -n 's/^nightshift: dispatched \([a-z0-9_]\{4,\}\).*/\1/p' | head -1)"
if [[ -z "${TASK_ID:-}" ]]; then
    echo
    echo "ERROR: could not parse task_id from dispatch output. Aborting." >&2
    exit 3
fi
echo "task_id: $TASK_ID"

# Step 2: wait, then pause
echo
echo "==> waiting ${PAUSE_DELAY}s before pause"
sleep "$PAUSE_DELAY"
PAUSE_OUTPUT="$(run_step "/nightshift-pause $TASK_ID" "/nightshift-pause $TASK_ID")"
PAUSE_TS="$(date -u +%s)"

# Step 3: stage the injection
INJECT_OUTPUT="$(run_step "/nightshift-inject $TASK_ID \"$INJECT_TEXT\"" "/nightshift-inject $TASK_ID \"$INJECT_TEXT\"")"
INJECT_TS="$(date -u +%s)"

# Step 4: resume (Week 3 path: re-dispatch with prior transcript as context)
RESUME_OUTPUT="$(run_step "/nightshift-resume $TASK_ID" "/nightshift-resume $TASK_ID")"
RESUME_TS="$(date -u +%s)"

# Step 5: wait, then status
echo
echo "==> waiting ${COMPLETE_DELAY}s before final status"
sleep "$COMPLETE_DELAY"
STATUS_OUTPUT="$(run_step "/nightshift-status $TASK_ID" "/nightshift-status $TASK_ID")"
END_TS="$(date -u +%s)"

# Step 6: archive
SRC_RUN_DIR="$HERMES_HOME/nightshift/runs/$TASK_ID"
if [[ -d "$SRC_RUN_DIR" ]]; then
    cp -r "$SRC_RUN_DIR" "$DEST/$TASK_ID"
else
    echo
    echo "WARNING: live run dir not found at $SRC_RUN_DIR"
fi

# Copy the parent transcript if a child run was created during resume
CHILD_ID="$(printf '%s\n' "$RESUME_OUTPUT" | sed -n 's/.*dispatched \([a-z0-9_]\{4,\}\)\(-[0-9]\+\)\?.*/\1\2/p' | head -1)"
if [[ -n "${CHILD_ID:-}" && "$CHILD_ID" != "$TASK_ID" && -d "$HERMES_HOME/nightshift/runs/$CHILD_ID" ]]; then
    cp -r "$HERMES_HOME/nightshift/runs/$CHILD_ID" "$DEST/$CHILD_ID"
fi

# Write session.md
TOTAL_DURATION=$((END_TS - START_TS))
PAUSE_AT=$((PAUSE_TS - START_TS))
INJECT_AT=$((INJECT_TS - START_TS))
RESUME_AT=$((RESUME_TS - START_TS))

cat > "$DEST/session.md" <<EOF
# dogfood run — profile=$PROFILE run=$RUN_NUM

**Captured:** $(date -u +%FT%TZ)
**Profile:** $PROFILE
**Goal:** \`$GOAL\`
**Task ID:** \`$TASK_ID\`${CHILD_ID:+ (child: \`$CHILD_ID\`)}

## Timing

| Phase | t (seconds from start) |
|---|---|
| Dispatched | 0 |
| Pause requested | $PAUSE_AT |
| Injection staged | $INJECT_AT |
| Resume dispatched | $RESUME_AT |
| Final status captured | $((END_TS - START_TS)) |
| Total | $TOTAL_DURATION |

## Captured outputs

### dispatch

\`\`\`
$RUN_OUTPUT
\`\`\`

### pause

\`\`\`
$PAUSE_OUTPUT
\`\`\`

### inject

\`\`\`
$INJECT_OUTPUT
\`\`\`

### resume

\`\`\`
$RESUME_OUTPUT
\`\`\`

### status

\`\`\`
$STATUS_OUTPUT
\`\`\`

## What surprised me

> (You write this — 1-3 paragraphs after watching the live transcript.
> Pattern: did the run do what you hoped? Did it pause cleanly? Did the
> injection land? What would you change next time?)

## Honest answer

> Could you have done this with the manual workflow, or did the plugin
> win your time back? (Yes / No / Mixed — why?)

EOF

echo
echo "==> archived to $DEST"
ls -la "$DEST"
