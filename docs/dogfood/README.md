# dogfood — 3 real runs for issue #4 acceptance

This directory holds artifacts from three real `/nightshift` runs that
the author (you) does as a real user, on real tasks, instead of
manually. Issue #4 sets this as Week 3 acceptance: a go/no-go decision
about whether to keep building the plugin or pivot.

**The runs are real** — they call `hermes chat -q "/nightshift …"`,
which loads the plugin through the same plugin loader any user would
hit. Transcripts come from `~/.hermes/nightshift/runs/<task_id>/` and
copy verbatim into `./<profile>-<n>/<task_id>/` so the git history
captures what you actually saw, not paraphrased notes.

## How to do a run

```bash
# Profile names match the three tasks in issue #4:
#   research     — survey a domain you don't already know
#   code-review  — review a real PR or branch in your own project
#   synthesis    — draft a summary from your own notes folder
#
# Args:        <profile> <1..3> "<goal>"
# Use path:    scripts/dogfood.sh <profile> <n> "<goal>"

# Example — first research run
scripts/dogfood.sh research 1 \
  "Survey 5 AI agent business models worth pursuing in 2026"

# Example — first code review
scripts/dogfood.sh code-review 1 \
  "Review PR #4 in my own hermes-nightshift repo: are the three rerun
   flags (dispatching/pause-requested/running) the right state machine?"
```

Each run takes about **2 minutes wall-clock** (30s pause, 60s
complete, plus model time). The run also pauses the live sub-agent at
30s, stages a real injection, and re-dispatches via the Week 3 resume
path — that's the whole point: test the full control loop on a real
task, not just the dispatch step.

## What to watch while it runs

The run prints `transcript: /…/nightshift/runs/<task_id>/transcript.md`
on dispatch. Open a second terminal and:

```bash
tail -F ~/.hermes/nightshift/runs/<task_id>/transcript.md
```

The accepted "watching UX" (per `docs/viewer-survey.md` and the Week 3
README section) is `tail -F` on the run dir — the mirror is keyed on
`task_id`, not on the core's `delegation_id`, so the tail is stable
across pause/resume/inject/re-dispatch.

## Acceptance checklist (from issue #4)

For each of the three runs, fill in `session.md`:

- [ ] **How long it ran** (timing block — pre-filled by the script)
- [ ] **What was visible in the transcript during the run** (paste 5-10
      lines of interesting turns)
- [ ] **How many times I intervened** (pause / inject / resume counts;
      the script does at least one of each on your behalf, add any extras)
- [ ] **Whether the final output was usable, or needed rework** (1
      sentence)
- [ ] **Honest answer to: would I have used the manual workflow instead?**
      (1-2 sentences)

After all three runs:

- [ ] A 1-page write-up of **what surprised me** is posted as a PR
      discussion or issue comment on `#1`
- [ ] **A go/no-go decision**: continue to Week 4 (`#5`), or pivot

## Why a script and not a checklist

A hand-driven checklist tempts you to skip the pause/inject/resume
loop when the model's first answer looks "good enough." Issue #4 is
not asking whether `/nightshift "PONG"` works — it already does. It is
asking whether **the whole control loop** (dispatch → observe →
correct → re-dispatch → finalize) holds together when the goal is
open-ended. The script forces one full cycle of each so you can answer
that question honestly.
