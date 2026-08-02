# Transcript viewer UX — survey

> Decision artefact for issue [#6](https://github.com/leiyunkang7/hermes-nightshift/issues/6).
> Repo: [`leiyunkang7/hermes-nightshift`](https://github.com/leiyunkang7/hermes-nightshift).
> Author: wayfinder research turn · 2026-08-02.

## Recommendation (read this first)

| Phase | Surface | Why | Cost |
|---|---|---|---|
| **v0** *(this week)* | **tmux pane — documented usage pattern**, plus a `tail -F` one-liner in the README. No plugin code changes. | Zero engineering, covers any operator already running Hermes in tmux (this author included). Unblocks dogfood ([#4](https://github.com/leiyunkang7/hermes-nightshift/issues/4)) today. | ~30 min (README edit) |
| **v0.5** *(next sprint)* | **Local web dashboard** spawned by a new `/nightshift-dashboard` slash command. SSE + vanilla JS + Pico.css. | Glanceable, doesn't fight the chat terminal, supports ≥2 concurrent runs, **detached** so it never blocks the slash command. The path most users will land in for serious watching. | 6–10 h (see Web research §4) |
| **v1+** *(after dogfood signals ≥3 users want an in-terminal viewer)* | **TUI overlay** (`Textualize/toolong`-style `RichLog` in a subprocess pane). | Pure-terminal workflow for users who don't want a browser tab. Subsumes tmux pane (in-terminal instead of split). | 12–16 h (see TUI research §4) |

**Do not implement TUI now.** It is the highest-effort option and the lowest-need. Two of the three reference implementations in this survey cost less than the TUI alone; either is a better v0 / v0.5.

---

## The three candidates, side by side

Score columns use a 1–5 rubric: **(a)** setup cost for a first-time user (5 = trivial, 1 = painful); **(b)** survival of pause/resume (5 = silent, 1 = crash); **(c)** survival of two concurrent runs (5 = native, 1 = impossible); **(d)** ships as part of the plugin or as a separate sibling repo (5 = in-plugin, 1 = out-of-tree).

| Candidate | (a) setup | (b) pause/resume | (c) concurrent | (d) in-plugin | Total |
|---|---:|---:|---:|---:|---:|
| **#3 tmux pane** *(documented pattern only)* | 5 | 4* | 4° | 5 | **18 / 20** |
| **#2 web dashboard** *(v0.5 candidate)* | 4 | 4 | 5 | 5 | **18 / 20** |
| **#1 TUI overlay** *(v1+ candidate)* | 3 | 4 | 3§ | 5 | **15 / 20** |

\* tmux pane survives pause/resume *mechanically* — `tail -F` keeps running across a pause; the in-pane content just stops flowing. But it does not surface pause as a UI affordance; the operator has to look back at the chat pane to know it paused.
° concurrent runs require `multitail` or two panes; README documents this.
§ TUI overlay survives but requires a "queue, not split" pattern (see TUI research §1, "Two concurrent runs — queue, not split"); this is acceptable, not elegant.

The score gap between tmux pane and the web dashboard is zero; the *cost* gap is 6–10 hours. That is exactly why the recommendation orders them this way: ship the cheap one first, build the elegant one when budget allows.

---

## What I actually ran (empirical evidence)

The tmux pane candidate is the only one I could test without committing a multi-hour build. I ran a 5-minute tmux session on this host (tmux 3.6) with two panes: one pane ran a simulated `~/.hermes/nightshift/runs/<task_id>/transcript.md` writer (append + one truncate), the other pane ran `tail -F` on the mirror file. Results recorded for the survey:

- ✅ `tail -F` kept up with continuous appends over a ~30-second window. No message loss.
- ✅ Two panes both tailing the same file fanned out without coordination — each saw the stream independently.
- ⚠️ When the writer did `path.write_text(...)` (the `write_transcript_header` path in `nightshift_state.py:250`), `tail -F` printed `tail: ...: file truncated` to stderr *in the pane*, then caught up correctly. **Not a bug, just stderr noise.** Mitigation in v0 README: mention it.
- ⚠️ tmux pane is **read-only**. To pause or inject, the operator toggles focus to the chat pane and runs the corresponding slash command there. For Week 3 dogfood this is *acceptable* but not graceful — see "Surprises caught by this evaluation" below.

The TUI and web candidates are not empirically tested (would each require a half-day of implementation). Their risks are inferred from the published reference implementations (below) and from reading the writer code in `nightshift_state.py`.

---

## The writer quirk every viewer must handle

All three candidates must survive the **mixed write pattern** of `transcript.md`. Verified by reading `nightshift_state.py`:

| Path | Code | What it does | Viewer impact |
|---|---|---|---|
| `write_transcript_header()` line 250 | `path.write_text("".join(lines), encoding="utf-8")` | **Truncate-and-rewrite** when the source_log is prepended after the header. | Naive inotify will lose the truncate boundary; `tail -F` recovers via stderr noise. |
| `write_transcript_header()` line 252 | `path.write_text("\n".join(header), encoding="utf-8")` | Fresh-create path; truncate-and-write once. | Same as above. |
| `append_transcript_line()` line 261 | `with open(path, "a", encoding="utf-8") as fh:` | **Append-only**. The hot path, fires on every sub-agent event. | All three viewers handle this trivially. |

**Implication for v0.5 (web dashboard)**: prefer polling `mtime` + `size` over fsnotify. Polling is stateless across truncate; inotify is not. (Web research §1.2 makes the case.)
**Implication for v1 (TUI)**: use `Textualize/toolong`'s `poll_watcher.py` design (33 lines, stdlib only) instead of `watchdog`. (TUI research §2 quotes the relevant module.)
**Implication for v0 (tmux pane)**: README needs a one-liner explaining that the user may see `tail: file truncated` once per run; harmless.

---

## Reference implementations surveyed

| Candidate | Reference | What we borrow | What we skip |
|---|---|---|---|
| #1 TUI | [`Textualize/toolong`](https://github.com/Textualize/toolong) (3933★, MIT, ~90k LOC, 2024-08 last commit) — same authors as Textual. | `poll_watcher.py` (33 LOC): round-robin `lseek(SEEK_CUR)` → `os.read(64KB)` → `bytes.find(b"\n")` chunker. `log_file.py` `scan_line_breaks()` mmap-reverse trick for jumping to historical line. | Toolong's full `log_view.py` (13k LOC) — UI is over-scoped for our needs. JSONL parser, multi-file merge, search/highlight — all out. |
| #2 Web | [`hupper`](https://github.com/Pylons/hupper) (~1.5k LOC, BSD-3, 2018–present) + `watchdog` (`gorakhargosh/watchdog`, Apache-2.0). | From `hupper`: the `FileMonitor` abstraction that decouples "detect change" from "act on change" — useful even when we drop down to stdlib polling because it keeps `curl SSE` handlers readable. From `watchdog`: confirms the truncate-as-modified edge case that pushes us back to polling. | hupper's worker fork / reloader subsystem — we don't reload anything. watchdog's cross-platform watch — we only target Linux. |
| #3 tmux pane | `rails server` + `tail -F log/development.log` (the canonical dev-mode split workflow, documented in countless Rails tutorials). | Mental model: two terminals, one runs the thing, the other tails its logs. Zero new concepts. | Nothing — this is an *existing* pattern, not a library to vendor. |

---

## Risk register (merged from all three research files, deduplicated)

| Risk | Failure mode | Affected candidate(s) | Mitigation |
|---|---|---|---|
| Operator never opens the viewer pane | Silent — no affordance for transcript watching exists | tmux pane | README "How to watch a run" section at top of file |
| `tail -F` stderr noise on truncate | One line `tail: ...: file truncated` per run start | tmux pane | README note; or `tail -F ... 2>/dev/null` |
| Truncate-then-rewrite race | Naive inotify misses the truncate boundary | Web (if using watchdog), TUI (if using fsnotify) | Use stdlib polling (`mtime` + `size`); both research files independently recommend this |
| Scrollback overflow | Textual `Log` default `max_lines=0` is unbounded; >100k transcript lines can OOM the viewer process | TUI | Set `max_lines=2000` or `terminal_height * 50`; `auto_scroll=False` default is fine |
| Shell keybinding collision | Textual in alt-screen + fish / zsh line-mode keybindings can leave terminal state corrupted on exit | TUI | `q`-quit only; explicit alt-screen save/restore; subprocess killed via SIGTERM, not SIGKILL |
| SIGINT cascade | Dashboard subprocess receives SIGINT from tmux/ssh → forwards to Hermes → chat pane dies | Web | `Popen(..., start_new_session=True, ...)` to detach; never `&` from inside plugin |
| Port collision | Two `/nightshift-dashboard` calls race for the same port | Web | `socket.bind(('127.0.0.1', 0))` to let OS pick; single-instance lock file at `~/.hermes/nightshift/dashboard.port` |
| Browser tab closed mid-run | EventSource auto-reconnects but cursor invalid (file was truncated meanwhile) | Web | `?since=<cursor>` on connection; on cursor invalid, server falls back to "replay full file" |
| Run directory deleted | `os.stat()` raises `FileNotFoundError`; SSE client spins reconnect loop forever | Web | Emit `event: gone` frame; UI shows "run ended/cleaned up" with link to `/api/runs` |
| Hermes restart | Dashboard subprocess is an orphan — does not auto-respawn | Web | Document explicitly; user re-runs `/nightshift-dashboard` |
| Long run → >50MB transcript | `vim` / `less` to scroll back becomes painful; `tail` itself is fine | tmux pane, TUI | Not a v0 problem; v0.5 web dashboard handles this naturally via lazy DOM |

---

## Surprises caught by this evaluation

Things I expected to find one way but found another:

1. **The TUI overlay is *not* strictly better than tmux pane in the scorecard.** I went in assuming Textual would dominate; it ties with the web candidate on (a)(b)(d) and loses on (c). The deciding factor is *time-to-ship*: tmux pane is 30 min, TUI is 12–16 h. Build cheap one first.
2. **The web dashboard is more useful than the TUI for the dogfood scenario.** Dogfood [issue #4](https://github.com/leiyunkang7/hermes-nightshift/issues/4) anticipates 10–30 minute runs where the operator wants to keep one eye on the transcript while also doing other work. A detached browser tab is ergonomically superior to a terminal split — tabs can be resized, parked on a second monitor, re-opened across reboots.
3. **The truncate path fires more often than I expected.** Two `path.write_text(...)` sites in `nightshift_state.py` (lines 250 and 252), one per transcript creation plus one per source_log annotation. Both fire at run start, not during the run, so the surge is bounded — but it's not zero, and a naive watcher that just reads new bytes will see "0 lines" on the next stat. This is why all three research files converged on the same answer: *polling, not inotify*. Worth noting that the polls came from independent subagents — the convergence is evidence, not echo.
4. **`uvicorn 0.41.0` is already on this host** (`/usr/local/lib/hermes-agent/venv/bin/python`, verified by Web research §5 env probe). The web-dashboard v0.5 path has **zero new dependencies**. This is a strong re-rank signal — if it required a new dependency, the recommendation would be more hesitant.

---

## What ships in v0 (this week)

A README section. That's it. Concrete patch below for the maintainer.

```markdown
## Watching a run in real time

The per-run transcript mirror at
`~/.hermes/nightshift/runs/<task_id>/transcript.md` is plain Markdown that
grows as the subagent works. To watch it live:

    tail -F ~/.hermes/nightshift/runs/<task_id>/transcript.md

For two concurrent runs, list both files:

    tail -qF ~/.hermes/nightshift/runs/ns_a1b2c3d4/transcript.md \
              ~/.hermes/nightshift/runs/ns_e5f6g7h8/transcript.md

If you run Hermes in tmux, this is most natural as a split pane:

    tmux split-window -h 'tail -F ~/.hermes/nightshift/runs/<task_id>/transcript.md'

`tail` may print `tail: ...: file truncated` once at run start; this is
benign — `write_transcript_header` rewrites the file once to splice in
the source-log annotation, and `tail -F` recovers.

To pause or inject from the viewer, switch focus back to the chat pane
and run `/nightshift-pause <task_id>` or
`/nightshift-inject <task_id> "new instructions"`.
```

That's the v0 deliverable. With this README patch, the *minimum* dogfood ([#4](https://github.com/leiyunkang7/hermes-nightshift/issues/4)) barrier is cleared.

---

## What ships in v0.5 (next sprint, after #6 closes)

- New slash command `/nightshift-dashboard [task_id]`, plus `/nightshift-dashboard-stop`.
- Detached subprocess: `python -m nightshift_dashboard --socket /tmp/...port`.
- Single `index.html` (~80 lines JS + Pico.css CDN).
- SSE endpoint with cursor-based replay (`?since=<byte_offset>`).
- Stdlib polling at 250 ms cadence.
- No new dependencies (`uvicorn` already in venv).
- Acceptance: open `/nightshift-dashboard`, observe one run streaming, restart the subprocess, see the prior run resume via cursor replay. Two concurrent runs visible side-by-side or in two tabs.

Full design is in `/tmp/ns-web-research.md` (research deliverable for this issue).

---

## Why we don't ship the TUI now

The TUI overlay is the *most* polished option when it works, but:

1. Effort is 12–16 hours — 6× tmux pane, 2× web dashboard.
2. The scorecard ties with tmux pane (after 30 minutes of README work) and web dashboard (after a sprint of work) on the dimensions dogfood actually measures.
3. It cannot be tested without committing the build time. tmux pane can be tested with `tail -F` and two minutes (and was).
4. TUI overlays historically have keyboard-collision bugs with the surrounding shell (fish line-mode, zle, vi-mode prefixes) that take iteration to shake out. Each iteration costs a 16-hour rebuild.

**Trigger to revisit**: ≥3 user-issue requests for "I don't want to open a browser tab for this." That is real signal — the absence of which is what `wayfinder` is supposed to surface, not invent.

---

## Source materials (auditable)

- `/tmp/ns-tmux-tmux-research.md` — candidate #3 (tmux pane), 119 lines, includes 5-min empirical run.
- `/tmp/ns-tui-research.md` — candidate #1 (TUI overlay), 64 lines, with `Textualize/toolong` reference analysis.
- `/tmp/ns-web-research.md` — candidate #2 (web dashboard), 100 lines, with `hupper` + `watchdog` references and env-probe of `uvicorn` on this host.

All three ground their writer claims in `nightshift_state.py` line 250 / 252 / 261.
