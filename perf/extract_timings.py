#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Extract per-command proof timings from an Isabelle session build DB.

A normal ``isabelle build`` persists per-command wall-clock timings into the
session build database, in the ``isabelle_session_info.command_timings`` blob
(YXML records carrying ``name`` / ``file`` / ``offset`` / ``elapsed``). This
script reads them via the existing ``ir/heap_info.py`` reader, groups them by
source file (theory), resolves offsets to source lines, and emits a
deterministic JSON artifact for the perf-reporting bot to compare across PRs.

We deliberately reuse ``HeapInfo`` rather than re-parse the blob: the zstd/YXML
decoding, the ``isabelle_sources`` digest check, and the symbol-offset->line
mapping all already live there and are exercised by the I/R tooling.

Usage:
    python3 perf/extract_timings.py --session AutoCorrode \\
        --isabelle "$ISABELLE_HOME/bin/isabelle" --out timings.json

Schema (schema=1):
    {
      "schema": 1,
      "session": "AutoCorrode",
      "commit": "<sha or ''>",
      "total_elapsed_s": 305.0,
      "theories": {
        "<theory display name>": {
          "file": "<repo-relative or symbolic source path>",
          "elapsed_s": 42.1,
          "commands": [
            {"entity": "lemma foo_wf", "name": "by", "line": 142,
             "elapsed_s": 8.7},
            ...
          ]
        }, ...
      }
    }

Theories are keyed by a display name derived from the source file; the
symbolic/resolved ``file`` is retained for disambiguation. Commands within a
theory are sorted by line then elapsed; theories are emitted in sorted order,
so byte-identical inputs yield byte-identical output (stable diffs).
"""

import argparse
import json
import os
import re
import sys

# Reuse the existing heap-DB reader (blob decode, source digest, offset->line).
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, os.pardir, "ir"))

import heap_info  # noqa: E402  (path set above)


# Isabelle commands that introduce a named entity we want to attribute proof
# time to. The record's own ``name`` is the command keyword (``by``, ``lemma``,
# ...), not the lemma name, so we recover the enclosing entity by scanning the
# source backwards from the command offset to the nearest such declaration.
_ENTITY_KEYWORDS = (
    "lemma", "theorem", "corollary", "proposition", "definition", "fun",
    "function", "primrec", "primcorec", "inductive", "coinductive",
    "datatype", "codatatype", "record", "instance", "instantiation",
    "termination", "abbreviation", "typedef", "locale", "class", "sublocale",
    "interpretation",
)
_ENTITY_RE = re.compile(
    r"^\s*(" + "|".join(_ENTITY_KEYWORDS) + r")\b[ \t]*([A-Za-z0-9_'.]+)?")

# Proof-step keywords: a timed command with one of these names is part of the
# proof of the preceding declaration, so we attribute it to that entity. A
# command whose name is itself a declaration keyword IS the entity. Any other
# timed command (ML, declare, text, ...) belongs to no named entity — we return
# None rather than mis-attributing it to whatever happened to precede it.
_PROOF_KEYWORDS = frozenset((
    "by", "apply", "apply_end", "done", "qed", "proof", "using", "unfolding",
    "supply", "subgoal", "next", "then", "show", "have", "hence", "thus",
    "obtain", "fix", "assume", "case", ".", "..",
))


def _theory_display_name(source_name, depth=2):
    """Human-friendly theory name from a source path, using the last ``depth``
    path components.

    ``$AUTOCORRODE_BASE/Micro_Rust_Runtime/Foo.thy`` -> ``Micro_Rust_Runtime.Foo``.
    ``depth`` is widened by the caller to break collisions. Purely cosmetic —
    the ``file`` field is the stable identifier; theories are keyed by file.
    """
    base = source_name
    for prefix in ("$ISABELLE_PROJECT_BASE/", "$AUTOCORRODE_BASE/", "~~/src/"):
        if base.startswith(prefix):
            base = base[len(prefix):]
            break
    if base.endswith(".thy"):
        base = base[:-4]
    parts = [p for p in base.split("/") if p and not p.startswith("$")]
    if not parts:
        return source_name
    return ".".join(parts[-depth:])


def _verified_path(checker, digests, source_name):
    """Resolved filesystem path for ``source_name`` iff its digest matches the
    build, else None. Uses the public ``SourceChecker.check`` (returns
    ``(path, matches)``) rather than reaching into its private cache."""
    digest = digests.get(source_name)
    if digest is None:
        return None
    path, matches = checker.check(source_name, digest)
    return path if matches is True else None


def _entity_at(checker, digests, source_name, offset, cmd_name):
    """Best-effort: the named entity this timed command belongs to, or None.

    ``cmd_name`` is the command keyword from the timing record. We attribute
    only when it is meaningful (see ``_PROOF_KEYWORDS``): a proof step maps to
    the preceding declaration; a declaration keyword is its own entity;
    everything else (``ML``, ``declare``, ...) returns None rather than being
    mis-attributed to whatever declaration happened to precede it.

    Only attempted when the source is present and its digest matches the build
    (otherwise line/offset math is meaningless). Returns e.g. "lemma foo_wf".
    """
    if cmd_name not in _PROOF_KEYWORDS and cmd_name not in _ENTITY_KEYWORDS:
        return None
    path = _verified_path(checker, digests, source_name)
    if path is None:
        return None
    lines = _read_lines(path)
    if lines is None:
        return None
    line_no = _offset_to_line(path, offset) or 0
    # Scan upward from the command's line for the nearest entity declaration.
    for i in range(min(line_no, len(lines)) - 1, -1, -1):
        m = _ENTITY_RE.match(lines[i])
        if m:
            kw, nm = m.group(1), m.group(2)
            return kw + (" " + nm if nm else "")
    return None


def _offset_to_line(path, offset):
    """``heap_info.offset_to_line`` but tolerant of a non-numeric offset (the
    blob is external data): returns None instead of raising ValueError."""
    try:
        return heap_info.offset_to_line(path, offset)
    except (ValueError, TypeError):
        return None


_lines_cache = {}


def _read_lines(path):
    if path not in _lines_cache:
        try:
            _lines_cache[path] = open(path, "r", errors="replace").read().splitlines()
        except OSError:
            _lines_cache[path] = None
    return _lines_cache[path]


def _assert_timings_decodable(info, session):
    """Raise SystemExit if the command_timings blob is present but undecodable.

    heap_info.decompress_blob returns None specifically when the blob is
    zstd-compressed (the normal case for a real build) but the `zstandard`
    module is unavailable. We must not confuse that with an empty blob.
    """
    row = info._conn.execute(
        "SELECT command_timings FROM isabelle_session_info WHERE session_name=?",
        (session,)).fetchone()
    blob = row[0] if row else None
    if not blob:
        return  # genuinely no timing blob — a real (if surprising) empty build
    if heap_info.decompress_blob(blob) is None:
        raise SystemExit(
            "command_timings blob is present ({} bytes) but could not be "
            "decoded — the build recorded timings, so this is a tooling "
            "problem, not an empty build.\n"
            "Almost certainly the Python 'zstandard' module is missing (the "
            "blob is zstd-compressed). Install it in the extraction "
            "environment: pip install zstandard  (or: pip install "
            "-r ir/requirements.txt).".format(len(blob)))


def extract(session, isabelle_bin, commit=""):
    """Build the timings dict for ``session``. Raises on unrecoverable errors;
    returns a schema-1 dict (possibly with no theories if the blob is empty)."""
    info = heap_info.HeapInfo.discover(session, isabelle_bin)
    if info is None:
        raise SystemExit(
            f"no build DB found for session {session!r}; build it first "
            f"(isabelle build -b -d . {session})")

    # Distinguish "the build genuinely recorded no slow commands" from "we could
    # not DECODE the timing blob" (almost always: the `zstandard` module is
    # missing, so heap_info.decompress_blob returns None on a zstd blob). The
    # latter must fail loudly — otherwise we silently emit 0 theories and the
    # bot reports a bogus "no data" / "no change".
    _assert_timings_decodable(info, session)

    records = info._ensure_timing_records()  # already filters system sources
    checker = info._ensure_checker()
    # name -> digest, for the public SourceChecker.check(name, digest) API.
    digests = dict(checker.rows)

    # Group by the SOURCE FILE (unique key), not the display name: two files in
    # different session dirs can share a "<dir>.<theory>" display name and would
    # otherwise merge, corrupting per-theory totals. We assign display names
    # after grouping and disambiguate any collisions deterministically.
    by_file = {}
    for r in records:
        source = r.get("file", "")
        if not source:
            continue
        try:
            elapsed = float(r.get("elapsed", "0"))
        except (ValueError, TypeError):
            continue
        offset = r.get("offset", "")
        line = 0
        if offset:
            # get_line -> offset_to_line can raise on a non-numeric offset.
            try:
                line = checker.get_line(source, offset) or 0
            except (ValueError, TypeError):
                line = 0
        cmd_name = r.get("name", "?")
        th = by_file.setdefault(source, {"elapsed_s": 0.0, "commands": []})
        th["elapsed_s"] += elapsed
        th["commands"].append({
            "entity": _entity_at(checker, digests, source, offset, cmd_name)
            if offset else None,
            "name": cmd_name,
            "line": line,
            "elapsed_s": round(elapsed, 4),
        })

    # Assign display names; disambiguate collisions by widening the path suffix,
    # deterministically (sorted by file) so runs are reproducible.
    display = {}
    used = {}
    for source in sorted(by_file):
        name = _theory_display_name(source)
        if name in used and used[name] != source:
            name = _theory_display_name(source, depth=3)
            suffix = 2
            base = name
            while name in used and used[name] != source:
                name = f"{base}#{suffix}"
                suffix += 1
        used[name] = source
        display[source] = name

    # Deterministic ordering: theories by display name; commands by (line, -s).
    ordered = {}
    total = 0.0
    for source in sorted(by_file, key=lambda s: display[s]):
        th = by_file[source]
        th["commands"].sort(key=lambda c: (c["line"], -c["elapsed_s"]))
        entry = {"file": source, "elapsed_s": round(th["elapsed_s"], 4),
                 "commands": th["commands"]}
        total += entry["elapsed_s"]
        ordered[display[source]] = entry

    return {
        "schema": 1,
        "session": session,
        "commit": commit,
        "total_elapsed_s": round(total, 4),
        "theories": ordered,
    }, info


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", default="AutoCorrode",
                    help="session name (default: AutoCorrode)")
    ap.add_argument("--isabelle", default=os.environ.get("ISABELLE_TOOL", "isabelle"),
                    help="path to the isabelle binary")
    ap.add_argument("--commit", default=os.environ.get("GITHUB_SHA", ""),
                    help="commit sha to record in the artifact")
    ap.add_argument("--out", required=True, help="output JSON path")
    ap.add_argument("--no-hotspots", action="store_true",
                    help="skip the human-readable hotspots dump on stdout")
    args = ap.parse_args(argv)

    data, info = extract(args.session, args.isabelle, args.commit)

    with open(args.out, "w") as f:
        json.dump(data, f, indent=1, sort_keys=False)
        f.write("\n")

    n_cmds = sum(len(t["commands"]) for t in data["theories"].values())
    print(f"wrote {args.out}: {len(data['theories'])} theories, "
          f"{n_cmds} timed commands, total {data['total_elapsed_s']:.1f}s")

    if not args.no_hotspots:
        try:
            print()
            print(info.timing_hotspots(top_n=20))
        except Exception as e:  # hotspots are a nicety; never fail the run
            print(f"(hotspots unavailable: {e})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
