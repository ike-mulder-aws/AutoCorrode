#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Compare two timing artifacts and render a PR-comment report (S-MED).

Reads a baseline (from ``main``) and a current (the PR) ``timings.json`` as
produced by ``extract_timings.py`` and produces:

  * ``diff.json`` — the full machine-readable delta (uploaded as an artifact);
  * ``comment.md`` — the PR comment body, one of four variants.

Why "S-MED" (median-ratio normalization)
----------------------------------------
Isabelle's ``elapsed`` is wall-clock, and CI runs on shared runners, so every
measurement carries an unknown machine-speed factor ``m`` (a slow runner
inflates *everything*). To separate a real per-proof slowdown from ``m`` we:

  1. compute the ratio ``r_i = cur_i / base_i`` for each theory present in both;
  2. take ``m = median(r_i)`` as the robust estimate of the machine factor;
  3. judge each theory by its *normalized* ratio ``r_i / m``.

This cancels a uniform machine slowdown (nothing flags) and — unlike
share-of-total — is not distorted when theories are added or removed.

Known blind spot (surfaced, not hidden): a *broad* real slowdown (a shared simp
lemma, a common ancestor theory) moves the median with it and is mathematically
indistinguishable from a slow runner via wall-clock ratios alone. When ``m``
itself is far from 1 we therefore emit an explicit "broad slowdown — can't
attribute" notice (Variant C) rather than claim false confidence.

Per-command deltas are matched by entity name and are *informational only* —
never a flagging signal on their own (offsets/lines shift with edits).

Exit code is always 0 in bot mode. The gate predicate (``theory_regressions``)
is factored out so a future gate can exit non-zero on it without a redesign.
"""

import argparse
import json
import re
import statistics
import sys


MARKER = "<!-- perf-bot -->"
COMMENT_CAP = 65536  # GitHub hard limit on issue/PR comment length (chars)
MAX_CMD_ROWS = 10      # per mover, informational command rows kept in diff.json
# Below this many overlapping theories, the median machine factor is too weak a
# statistical basis to trust normalized per-theory ratios; report low-confidence.
MIN_OVERLAP = 3


# Theory / entity names come from the PR build's timings.json, which on a fork
# PR is fully attacker-controlled. They are interpolated into the PR comment
# body, so they must be neutralized before rendering: strip characters that let
# an attacker inject markdown/HTML, break table cells (`|`), or spam-notify via
# @mentions. We keep only a conservative identifier-ish set plus spaces.
_UNSAFE = re.compile(r"[^0-9A-Za-z_.'\- ]")


def safe(text):
    """Neutralize an untrusted name for inclusion in the markdown comment."""
    if text is None:
        return ""
    return _UNSAFE.sub("", str(text)).strip() or "?"


# --------------------------------------------------------------------------- #
# Core comparison
# --------------------------------------------------------------------------- #

def load(path):
    if not path:
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _load_changed_files(path):
    """Read the newline-delimited changed-file list, or return an empty set.
    Used only to mark ✎ / scope the gate — a bad/absent file just disables
    those, never errors."""
    if not path:
        return set()
    try:
        with open(path) as f:
            return {ln.strip().replace("\\", "/") for ln in f if ln.strip()}
    except OSError:
        return set()


def machine_factor(baseline, current):
    """Median per-theory ratio ``m`` over theories present (nonzero) in both."""
    ratios = []
    b_th = baseline.get("theories", {})
    c_th = current.get("theories", {})
    for name, b in b_th.items():
        c = c_th.get(name)
        if not c:
            continue
        bs, cs = b.get("elapsed_s", 0.0), c.get("elapsed_s", 0.0)
        if bs > 0 and cs > 0:
            ratios.append(cs / bs)
    if not ratios:
        return 1.0, 0
    return statistics.median(ratios), len(ratios)


def command_deltas(b_cmds, c_cmds):
    """Informational per-command deltas, matched by entity name.

    Returns (rows, worst) where rows is a list of dicts sorted by delta desc
    and worst is the single largest absolute increase (for the summary column).
    Commands with no entity are bucketed under their keyword+line as a fallback
    key so at least intra-theory matches line up when unedited.
    """
    def key(c):
        return c.get("entity") or f"{c.get('name','?')}@{c.get('line',0)}"

    b_by = {}
    for c in b_cmds:
        b_by.setdefault(key(c), 0.0)
        b_by[key(c)] += c.get("elapsed_s", 0.0)
    c_by = {}
    c_meta = {}
    for c in c_cmds:
        k = key(c)
        c_by.setdefault(k, 0.0)
        c_by[k] += c.get("elapsed_s", 0.0)
        c_meta[k] = c
    rows = []
    for k, cs in c_by.items():
        bs = b_by.get(k, 0.0)
        rows.append({
            "key": k,
            "entity": c_meta[k].get("entity"),
            "name": c_meta[k].get("name", "?"),
            "line": c_meta[k].get("line", 0),
            "base_s": round(bs, 3),
            "cur_s": round(cs, 3),
            "delta_s": round(cs - bs, 3),
            # Track presence separately from the rounded base_s: a command that
            # ran in <0.0005s baseline rounds to 0.000 but is NOT new.
            "base_present": k in b_by,
        })
    rows.sort(key=lambda r: -r["delta_s"])
    # Summary cell shows the single biggest grower (any positive delta).
    worst = rows[0] if rows and rows[0]["delta_s"] > 0 else None
    return rows, worst


def _theory_changed(entry, changed_files):
    """True if this theory's own source file is in the PR's changed-file set.
    The timing `file` is absolute/symbolic (e.g. /__w/.../Micro/Foo.thy) while
    the diff lists repo-relative paths (Micro/Foo.thy), so match when one path
    is a trailing-segment suffix of the other (a full path-component tail, not a
    bare substring, to avoid Foo.thy matching BarFoo.thy)."""
    if not changed_files:
        return False
    src = (entry.get("file") or "").replace("\\", "/")
    if not src:
        return False
    src_parts = src.split("/")
    for cf in changed_files:
        cf_parts = cf.split("/")
        n = min(len(src_parts), len(cf_parts))
        if n and src_parts[-n:] == cf_parts[-n:]:
            return True
    return False


def compare(baseline, current, opts, changed_files=None):
    """Return the full diff structure (also serialized to diff.json).

    Primary metric is the machine-adjusted absolute delta:
        adj = (cur - base) - (m - 1) * base
    i.e. the change in seconds after removing the expected runner-speed drift.
    The normalized ratio is retained as human context only.
    """
    m, m_n = machine_factor(baseline, current)
    b_th = baseline.get("theories", {})
    c_th = current.get("theories", {})

    theories = []
    for name in sorted(set(b_th) | set(c_th)):
        b = b_th.get(name)
        c = c_th.get(name)
        bs = b.get("elapsed_s", 0.0) if b else 0.0
        cs = c.get("elapsed_s", 0.0) if c else 0.0
        entry = {
            "theory": name,
            "file": (c or b or {}).get("file", ""),
            "base_s": round(bs, 3),
            "cur_s": round(cs, 3),
            "pr_adj_s": None,      # PR re-expressed at baseline runner speed
            "delta_s": round(cs - bs, 3),
            "adj_delta_s": None,   # machine-adjusted seconds (primary metric)
            "norm_ratio": None,    # (cur/base)/m — context only
            "status": "both",
            "changed": False,
        }
        if b is None:
            entry["status"] = "added"
            entry["adj_delta_s"] = round(cs, 3)  # all new time is "added"
        elif c is None:
            entry["status"] = "removed"
            entry["adj_delta_s"] = round(-bs, 3)
        elif bs > 0 and cs > 0:
            # PR at baseline runner speed: cur - (m-1)*base, so that
            # adj_delta = pr_adj - base holds exactly.
            entry["pr_adj_s"] = round(cs - (m - 1.0) * bs, 3)
            entry["adj_delta_s"] = round((cs - bs) - (m - 1.0) * bs, 3)
            entry["norm_ratio"] = round((cs / bs) / m if m > 0 else 0.0, 4)
        else:
            entry["adj_delta_s"] = round(cs - bs, 3)
        entry["changed"] = _theory_changed(entry, changed_files)
        theories.append(entry)

    # Per-command deltas for the theories we'll actually surface (changed ones
    # and the biggest movers) — computed lazily in render from the raw inputs.
    for entry in theories:
        b = b_th.get(entry["theory"])
        c = c_th.get(entry["theory"])
        if b and c and abs(entry["adj_delta_s"] or 0) >= opts.view_floor_s:
            rows, worst = command_deltas(
                b.get("commands", []), c.get("commands", []))
            entry["worst_command"] = worst
            entry["commands"] = rows[:MAX_CMD_ROWS]

    # Movers shown in the comment: |adjusted delta| over the view floor,
    # sorted by adjusted delta descending (biggest slowdowns first).
    movers = [t for t in theories if abs(t["adj_delta_s"] or 0) >= opts.view_floor_s]
    movers.sort(key=lambda t: -(t["adj_delta_s"] or 0))

    broad = abs(m - 1.0) > opts.broad_notice
    low_confidence = m_n < MIN_OVERLAP

    return {
        "machine_factor": round(m, 4),
        "machine_factor_n": m_n,
        "low_confidence": low_confidence,
        "base_total_s": round(baseline.get("total_elapsed_s", 0.0), 2),
        "cur_total_s": round(current.get("total_elapsed_s", 0.0), 2),
        "broad_slowdown": broad,
        "theories": theories,
        "movers": movers,
        "changed_files_known": bool(changed_files),
        "thresholds": {
            "view_floor_s": opts.view_floor_s,
            "broad_notice": opts.broad_notice,
        },
    }


def theory_regressions(diff):
    """Gate predicate for a future soft/hard gate: theories whose source the PR
    changed AND which slowed by at least the view floor. Changed-file scoping is
    what makes this trustworthy on a single noisy sample (an untouched theory
    moving ±10s is common); a gate should not fire on unattributable movement."""
    return [t for t in diff["movers"]
            if t["changed"] and (t["adj_delta_s"] or 0) >= diff["thresholds"]["view_floor_s"]]


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def _pct(norm_ratio):
    return f"{(norm_ratio - 1.0) * 100:+.0f}%" if norm_ratio is not None else "—"


def _artifacts_line(opts, extra=""):
    """The trailing provenance line. ``extra`` is appended inside the <sub>."""
    commit = f"@`{safe(opts.base_commit[:7])}`" if opts.base_commit else ""
    link = (f" · [raw timings ↓]({opts.run_url})" if opts.run_url
            else " · raw timings in workflow artifacts")
    return f"<sub>baseline: `main`{commit}{link}{extra}</sub>"


def _movers_table(movers):
    """Absolute-first movers table. ✎ marks PR-changed theories; `PR (adj)` is
    PR re-expressed at baseline runner speed (so Δ = PR(adj) − main); Δ is the
    machine-adjusted delta; % is context."""
    lines = ["| | Theory | main | PR | PR (adj) | **Δ** | % |",
             "|:--|---|--:|--:|--:|--:|--:|"]
    for t in movers:
        mark = "✎" if t["changed"] else ""
        name = safe(t["theory"])
        if t["status"] == "added":
            base, pct = "—", "*new*"
        elif t["status"] == "removed":
            base, pct = f"{t['base_s']:.1f}s", "*gone*"
        else:
            base, pct = f"{t['base_s']:.1f}s", _pct(t["norm_ratio"])
        cur = "—" if t["status"] == "removed" else f"{t['cur_s']:.1f}s"
        pr_adj = f"{t['pr_adj_s']:.1f}s" if t.get("pr_adj_s") is not None else "—"
        lines.append(f"| {mark} | {name} | {base} | {cur} | {pr_adj} | "
                     f"**{t['adj_delta_s']:+.1f}s** | {pct} |")
    return "\n".join(lines)


def _slowest_commands(current, changed_files, top=20):
    """The slowest individual commands in THIS build (absolute times — the
    hotspot dump the measure job used to print, now in the comment). Marks
    commands in PR-changed files with ✎."""
    cmds = []
    for entry in current.get("theories", {}).values():
        src = (entry.get("file") or "").replace("\\", "/")
        changed = _theory_changed(entry, changed_files)
        for c in entry.get("commands", []):
            cmds.append((c.get("elapsed_s", 0.0), c, src, changed))
    cmds.sort(key=lambda x: -x[0])
    if not cmds:
        return ""
    short = lambda s: s.split("/")[-1] if s else "?"
    lines = ["| time | command | location |", "|--:|---|---|"]
    for e, c, src, changed in cmds[:top]:
        loc = f"{short(src)}:{c.get('line', 0)}"
        if changed:
            loc = f"**{loc}** ✎"
        lines.append(f"| {e:.1f}s | `{safe(c.get('name','?'))}` | {loc} |")
    return "\n".join(lines)


def render_movers(diff, current, opts):
    """Proposal C: neutral totals header + absolute-first movers table, with a
    ✎ marker on PR-changed theories doing the noise disambiguation that a
    single-sample threshold cannot, plus a slowest-commands hotspot block."""
    d = diff
    m = d["machine_factor"]
    raw_total = d["cur_total_s"] - d["base_total_s"]
    # Adjusted total: raw change minus the drift expected from the runner-speed
    # factor on the whole baseline. Computed on the aggregate (not summed from
    # per-theory adjustments) so it doesn't accumulate per-theory rounding, and
    # so it matches the prose "raw, minus the runner gap".
    adj_total = raw_total - (m - 1.0) * d["base_total_s"]
    movers = d["movers"]
    # A large runner gap makes the whole comparison shaky, not just the totals.
    unreliable = abs(m - 1.0) > opts.broad_notice
    changed_slow = [t for t in movers
                    if t["changed"] and (t["adj_delta_s"] or 0) > 0]

    if unreliable:
        faster = "faster" if m < 1 else "slower"
        out = [MARKER,
               f"## ⚠️ Proof timing — runner ×{m:.2f} (~{abs(1-m)*100:.0f}% "
               f"{faster} than baseline); comparison unreliable",
               "",
               f"Raw total {raw_total:+.0f}s, ≈ {adj_total:+.0f}s after removing "
               f"the runner gap. A gap this large makes per-theory numbers noisy "
               f"— the ✎ row is where a real change is most likely, not a "
               f"measurement."]
    else:
        out = [MARKER,
               f"## ⏱️ Proof timing — {adj_total:+.0f}s adjusted "
               f"(raw {raw_total:+.0f}s, runner ×{m:.2f})"]

    if not movers:
        out += ["",
                f"No theory moved by ≥{opts.view_floor_s:.0f}s (adjusted). "
                f"See *How Δ is computed* below.",
                "", _artifacts_line(opts), "", _factor_explainer(d)]
        return "\n".join(out)

    legend = ("**✎ = source changed by this PR.** `PR (adj)` = PR at baseline "
              "runner speed, so `Δ = PR(adj) − main`; % is context. See *How Δ "
              "is computed* below.")
    out += ["", legend, "", _movers_table(movers)]

    if d["changed_files_known"] and changed_slow:
        bits = ", ".join(f"`{safe(t['theory'])}` {t['adj_delta_s']:+.1f}s"
                         for t in changed_slow[:3])
        more = f" (+{len(changed_slow)-3} more)" if len(changed_slow) > 3 else ""
        label = "theory" if len(changed_slow) == 1 else "theories"
        out += ["", f"**👉 {bits}{more}** — {label} this PR changed."]
    elif d["changed_files_known"]:
        out += ["", "**👉 No theory this PR changed slowed past the floor;** "
                "the movers below are in unchanged files."]

    hidden = len(d["theories"]) - len(movers)
    art = f"[artifact ↓]({opts.run_url})" if opts.run_url else "the artifact"
    out += ["",
            f"<sub>{len(d['theories'])} theories compared "
            f"({d['machine_factor_n']} present in both); only │Δ│ ≥ "
            f"{opts.view_floor_s:.0f}s shown — {hidden} under the floor hidden "
            f"(a ±40% swing on a sub-{opts.view_floor_s:.0f}s theory is "
            f"scheduling jitter). Full per-theory + per-command data in "
            f"{art}.</sub>"]

    out += ["", _factor_explainer(d)]

    # Slowest individual commands in this build (absolute), ✎-annotated.
    slow = _slowest_commands(current, opts.changed_files, top=20)
    if slow:
        out += ["", "<details><summary>Slowest individual commands "
                "(this build)</summary>", "", slow, "", "</details>"]
    return "\n".join(out)


def _factor_explainer(d):
    """Collapsed note explaining where the runner-speed factor comes from and
    how Δ is derived, with numbers from this run. Keeps the headline clean while
    making the metric auditable."""
    m = d["machine_factor"]
    n = d["machine_factor_n"]
    pctfast = (1.0 - m) * 100  # m<1 => PR runner faster than baseline runner
    faster_slower = "faster" if m < 1 else "slower"
    return "\n".join([
        "<details><summary>How Δ is computed (runner-speed adjustment)</summary>",
        "",
        "CI runners vary in speed run-to-run, so raw before/after seconds "
        "aren't directly comparable. We estimate this run's speed relative to "
        "the baseline and subtract it out, so Δ reflects a *real* change, not a "
        "faster/slower machine.",
        "",
        f"- **Runner-speed factor** `m = median(PRᵢ / mainᵢ)` over the {n} "
        f"theories built in both runs = **×{m:.3f}**. Using the median (not the "
        f"mean) makes it robust: a handful of genuinely-changed theories don't "
        f"skew it. Here m ≈ {m:.2f}, i.e. this PR's runner was ~{abs(pctfast):.0f}% "
        f"{faster_slower} overall.",
        "- **Adjusted Δ** per theory `= (PR − main) − (m − 1)·main`. The second "
        "term is the change we'd expect purely from runner speed; subtracting it "
        "leaves the genuine change. (So on a faster runner, an unchanged theory "
        "lands near Δ≈0 rather than looking like a speedup.)",
        "- **%** shown is the same idea as a ratio: `(PR/main)/m − 1`.",
        "",
        "Caveat: this cancels a *uniform* speed difference, not per-theory "
        "variance — an untouched theory can still drift several seconds, which "
        "is why the ✎ changed-file marker matters more than the number alone.",
        "</details>"])


def render_no_baseline(current, opts):
    th = current.get("theories", {})
    # Derive the denominator from the parts so shares are self-consistent even
    # if total_elapsed_s is stale/absent.
    total = sum(t.get("elapsed_s", 0.0) for t in th.values()) \
        or current.get("total_elapsed_s", 0.0)
    top = sorted(th.items(), key=lambda kv: -kv[1].get("elapsed_s", 0.0))[:10]
    out = [MARKER,
           "## ⏱️ Proof timing — recorded (no baseline to compare)",
           "No `main` timing artifact available yet, so this run only records "
           "timings. Once this lands on `main`, future PRs will compare "
           "against it.",
           "",
           f"Total build **{total:.0f}s** across {len(th)} theories.",
           "",
           _artifacts_line(opts),
           "",
           "<details><summary>Top 10 slowest theories (this build)</summary>",
           "",
           "| Theory | time | share |",
           "|---|--:|--:|"]
    for name, t in top:
        e = t.get("elapsed_s", 0.0)
        share = (e / total * 100) if total else 0.0
        out.append(f"| {safe(name)} | {e:.1f}s | {share:.1f}% |")
    out += ["", "</details>"]
    return "\n".join(out)


# A whole collapsed block (the full-theory table), matched non-greedily so we
# can drop the largest one intact rather than slicing through markup.
_DETAILS_RE = re.compile(r"\n*<details>.*?</details>", re.DOTALL)


def _cap(body):
    """Keep the comment under GitHub's hard limit WITHOUT slicing through
    markup. The only unbounded content is the collapsed full-theory <details>
    block (up to ~154 rows); if we overflow, drop whole <details> blocks
    (largest first) and point at the artifact, rather than truncating
    mid-table and emitting broken markdown. The MARKER stays intact (it is the
    first line and outside any block), so comment upsert still matches."""
    if len(body) <= COMMENT_CAP:
        return body
    notice = ("\n\n<sub>⚠️ full breakdown omitted to fit the comment size "
              "limit; see the `diff.json` artifact.</sub>")
    budget = COMMENT_CAP - len(notice)
    blocks = sorted(_DETAILS_RE.finditer(body), key=lambda m: -(m.end() - m.start()))
    for m in blocks:
        body = body[:m.start()] + body[m.end():]
        if len(body) <= budget:
            return body + notice
    # No <details> blocks left but still over (pathological): hard cut at a
    # newline boundary so we at least don't split a line mid-token.
    cut = body.rfind("\n", 0, budget)
    return body[:cut if cut > 0 else budget] + notice


def render_no_data(baseline):
    """Current build recorded no timings — almost always a build problem, not a
    genuine 'nothing changed'. Say so loudly rather than rendering a green
    all-clear."""
    msg = ("The current build recorded **no proof timings**. That usually means "
           "the build failed or produced no theories — check the "
           "[build logs](#) rather than reading this as 'no change'.")
    if baseline is not None:
        n = len(baseline.get("theories", {}))
        if n:
            msg += f" (The `main` baseline has {n} theories.)"
    return MARKER + "\n## ❓ Proof timing — no data from this build\n" + msg


def render_low_confidence(diff, opts):
    """Too few theories overlap for a trustworthy comparison (e.g. a large
    rename/move). Report totals + the movers we can still see, but flag that
    the runner-speed normalization is unreliable."""
    d = diff
    out = [MARKER,
           "## ⚠️ Proof timing — comparison inconclusive",
           f"Only **{d['machine_factor_n']}** theor"
           f"{'y' if d['machine_factor_n'] == 1 else 'ies'} overlap between this "
           f"PR and `main` (need ≥{MIN_OVERLAP}), so the runner-speed "
           f"normalization is unreliable. This usually means a large "
           f"rename/restructure. Totals: `main` **{d['base_total_s']:.0f}s** → "
           f"PR **{d['cur_total_s']:.0f}s**.",
           "",
           _artifacts_line(opts)]
    if d["movers"]:
        out += ["", _movers_table(d["movers"])]
    return "\n".join(out)


def render(baseline, current, diff, opts):
    if current is None:
        return _cap(render_no_data(baseline))
    # A build that produced no theories is a red flag, not a clean run — even if
    # a (also-empty) baseline exists. Guard before the comparison paths.
    if not current.get("theories"):
        return _cap(render_no_data(baseline))
    if baseline is None or not baseline.get("theories"):
        return _cap(render_no_baseline(current, opts))
    # Not enough overlap to trust normalized ratios — say so before movers.
    if diff["low_confidence"]:
        return _cap(render_low_confidence(diff, opts))
    return _cap(render_movers(diff, current, opts))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline", help="baseline timings.json (from main); "
                    "missing/unreadable => record-only report")
    ap.add_argument("--current", required=True, help="current PR timings.json")
    ap.add_argument("--out-md", required=True, help="output PR comment markdown")
    ap.add_argument("--out-diff", help="output full diff.json")
    ap.add_argument("--run-url", default="", help="workflow run URL for the "
                    "'raw timings' link")
    ap.add_argument("--base-commit", default="", help="baseline commit sha")
    ap.add_argument("--view-floor-s", type=float, default=2.0,
                    help="hide movers whose machine-adjusted |Δ| is under this "
                    "many seconds from the comment (data stays in the artifact)")
    ap.add_argument("--broad-notice", type=float, default=0.15,
                    help="note a broad runner-speed shift if |machine_factor-1| "
                    "exceeds this")
    ap.add_argument("--changed-files", default="",
                    help="path to a newline-delimited list of files the PR "
                    "changed; theories whose source is listed are marked ✎")
    ap.add_argument("--gate", action="store_true",
                    help="exit non-zero if a CHANGED theory slowed past the "
                    "floor (future gate mode; default is bot mode, always 0)")
    opts = ap.parse_args(argv)

    # Load the changed-file list (best-effort; absent => no ✎ markers).
    opts.changed_files = _load_changed_files(opts.changed_files)

    baseline = load(opts.baseline)
    current = load(opts.current)
    if current is None:
        sys.stderr.write(f"error: cannot read --current {opts.current!r}\n")
        return 2

    diff = None
    if baseline is not None:
        diff = compare(baseline, current, opts, changed_files=opts.changed_files)
        if opts.out_diff:
            with open(opts.out_diff, "w") as f:
                json.dump(diff, f, indent=1)
                f.write("\n")

    body = render(baseline, current, diff, opts)
    with open(opts.out_md, "w") as f:
        f.write(body)
        if not body.endswith("\n"):
            f.write("\n")

    if diff is not None:
        regressions = theory_regressions(diff)
        print(f"machine_factor=×{diff['machine_factor']:.2f} "
              f"movers={len(diff['movers'])} "
              f"changed_regressions={len(regressions)} "
              f"comment={len(body)}chars")
        if opts.gate and regressions:
            return 1
    else:
        print(f"no baseline; recorded-only report ({len(body)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
