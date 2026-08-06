<!-- SPDX-License-Identifier: MIT -->
# perf — proof-timing reporting bot

Surfaces, on every PR, how a change affects Isabelle proof performance — at the
session, theory, and (informationally) per-proof level. It **reports**; it does
not (yet) block merges. The goal is to let authors see the cost of a change and
reviewers catch slowdowns, while we collect real deltas to calibrate thresholds
before ever gating.

## How it works

1. **Measure** (`perf-measure.yml`, fork-safe): builds `AutoCorrode` exactly as
   CI already does, then runs `extract_timings.py` to pull per-command
   wall-clock timings out of the build DB into `timings.json`, uploaded as an
   artifact. Read-only token; never comments; safe for fork PRs.
2. **Report** (`perf-report.yml`, trusted): on measure completion, downloads the
   PR's `timings.json` and the latest **main** baseline artifact, runs
   `compare_timings.py`, and upserts a single PR comment via
   `report_comment.py`. Reads only JSON — never PR code — so it safely holds
   `pull-requests: write` (the fork-safe `workflow_run` pattern).

Pushes to `main` run only the measure stage — they publish the baseline.

## Where the timing comes from

Every normal `isabelle build` persists per-command wall-clock timings into the
session build DB (`isabelle_session_info.command_timings`, YXML records with
`name`/`file`/`offset`/`elapsed`). **No `record_theories`, no live PIDE session,
no IC2 required.** We read it through the existing `ir/heap_info.py` reader
(`HeapInfo.discover`, `_ensure_timing_records`, `SourceChecker` for offset→line)
rather than re-parsing the blob. CPU/GC are out of scope — wall-clock only.

The record's `name` is the command *keyword* (`by`, `lemma`, …), not the lemma
name, so `extract_timings.py` groups by source **file** and recovers a
best-effort **entity** (e.g. `lemma foo_wf`) by scanning the source backwards
from the command offset to the nearest declaration — only when the source digest
matches the build.

**Only slow commands are recorded.** Isabelle's build persists a timing only
for commands above a ~0.1s threshold (verified against a real AutoCorrode
build), so fast proofs don't appear individually and per-theory sums undercount
total build time. This is fine — and desirable — for regression detection: the
concern is the slow commands, and the blob stays small.

**Dependency note:** a real build's timing blob is **zstd-compressed**, so the
Python `zstandard` module is **required** to extract timings. The measure
workflow installs it via `apt-get install python3-zstandard` (the Isabelle
container has no pip) and fails if it can't; running `extract_timings.py`
without it errors loudly rather than silently reporting "0 theories". Install
locally with `pip install zstandard` (or your distro's `python3-zstandard`).

A real extracted sample (155 theories, 1158 commands) lives at
`perf/testdata/autocorrode_timings.sample.json` for local testing of
`compare_timings.py` without a build.

## The comparison metric: machine-adjusted absolute delta

Wall-clock on shared CI runners carries an unknown machine-speed factor `m` (a
slow runner inflates *everything*). We estimate it robustly as the **median
per-theory ratio** `m = median(cur_i / base_i)` over theories present in both
runs, then rank every theory by its **machine-adjusted absolute delta**:

```
adj_i = (cur_i − base_i) − (m − 1)·base_i     # seconds, runner-drift removed
```

**Absolute seconds are the signal; the percentage is only context.** A proof
that goes 54s → 78s (+24s) matters; one that goes 0.5s → 1.5s (+200%) does not.
The comment therefore sorts by `adj` descending and **hides anything with
`|adj| < --view-floor-s` (default 2s)** — the sub-2s theories where wall-clock
jitter produces meaningless ±40% swings. Hidden theories stay in the artifact.

**Why we don't auto-flag "regressions" from one run.** Empirically, an *untouched*
theory can move ±10s between runs (measured on AutoCorrode: `BraunTrees` moved
+10.9s / +41% with no source change). No absolute-or-relative threshold on a
single sample can include a real +24s change and exclude that noise. So the bot
does not render a confident "🔴 regression" verdict; it presents **the largest
movers and lets you judge** — with one extra signal that *does* discriminate:

**Changed-file attribution (✎).** The report workflow fetches the PR's changed
files (from the API, in trusted context) and marks movers whose source the PR
actually touched. A ✎ +24s mover is the story; an unmarked +11s mover is almost
certainly runner noise. This is what separates `Crush_Examples` (changed) from
`BraunTrees` (not) without any threshold tuning.

## Comment variants

- **Movers (normal):** neutral `+Ns total` header, then the absolute-first
  movers table (✎ = PR-changed), a 👉 line calling out changed theories that
  slowed, and a collapsed slowest-commands hotspot list. If nothing clears the
  floor, it says so in one line.
- **No baseline:** first run / no `main` artifact yet — records totals + top-10
  slowest, explains future PRs will compare.
- **Inconclusive:** fewer than 3 theories overlap (a large rename/restructure),
  so `m` is untrustworthy; reports totals + whatever movers it can, with a
  caveat.
- **No data:** the build produced no timings (likely a build failure); says so
  loudly rather than a false all-clear.

The comment shows only movers over the floor + a hotspot list; the full
per-theory and per-command data lives in the `timings.json` / `diff.json`
artifacts. A hard cap keeps the body under GitHub's 65,536-char limit by
dropping whole collapsed blocks (never slicing markup).

**Untrusted input.** On a fork PR the `timings.json` is attacker-controlled, so
theory/entity names are sanitized before rendering (strips `@`, backticks,
pipes, HTML) — no comment injection, @mention spam, or table breakage. The PR
number the bot comments on is resolved in the trusted report workflow from the
`workflow_run` head SHA, never from the untrusted artifact.

## Running locally

```bash
# 1. Build the session (populates the build DB).
isabelle build -b -d . -d $AFP_COMPONENT_BASE/Word_Lib AutoCorrode

# 2. Extract timings.
python3 perf/extract_timings.py --session AutoCorrode \
  --isabelle "$ISABELLE_HOME/bin/isabelle" --out timings.json

# 3. Compare against a baseline (e.g. a timings.json from main).
python3 perf/compare_timings.py --baseline baseline.json --current timings.json \
  --out-md comment.md --out-diff diff.json
cat comment.md
```

Pass `--changed-files FILE` (newline-delimited paths) to enable ✎ markers
locally. `compare_timings.py` accepts `--gate` to exit non-zero when a
**changed** theory slowed past the floor (off by default — bot mode always
exits 0). The gate predicate lives in one function (`theory_regressions`),
scoped to PR-changed theories so it won't fire on untouched-theory noise —
graduating to a soft/hard status check is a wiring change, not a redesign.

## Tuning thresholds

`compare_timings.py` flags: `--view-floor-s` (default 2.0 — the seconds below
which movers are hidden from the comment) and `--broad-notice` (0.15). Raise the
floor if 2s movers are still too noisy on your runners.

## Follow-ups (not yet implemented)

- **Graduate to a gate** once the floor is calibrated: wire `theory_regressions`
  (changed-theory slowdowns) to a non-required status check, then a required one
  for a curated hot-path allowlist.
- **Import-aware ✎:** also mark movers whose *imported* theories changed (needs
  the import graph), to catch shared-lemma slowdowns in unchanged files.
- **Multi-sample baselines:** accumulate N main runs and use per-theory
  median+MAD bands to distinguish a real slowdown from runner variance without
  relying on the changed-file heuristic.
