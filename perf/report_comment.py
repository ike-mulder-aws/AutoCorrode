#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Upsert the perf bot's PR comment via the GitHub REST API.

Finds the bot's existing comment (identified by a hidden marker) on the PR and
edits it, else creates a new one — so there is exactly one perf comment per PR,
updated on each push rather than stacked.

Runs from the trusted `perf-report.yml` workflow (never touches PR code, only a
markdown file + the GitHub API), so it may hold a write token safely.

Auth/config via env (all provided by Actions):
    GITHUB_TOKEN  — token with pull-requests: write
    GH_REPO       — "owner/repo" (default: $GITHUB_REPOSITORY)
Args:
    --pr N            PR number to comment on
    --body FILE       markdown body (must contain the marker; we also enforce it)
    --marker STR      identifying marker (default: "<!-- perf-bot -->")

Uses only the Python stdlib (urllib) — no extra deps on the runner.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


API = "https://api.github.com"
DEFAULT_MARKER = "<!-- perf-bot -->"


def _req(method, url, token, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def find_existing(repo, pr, token, marker):
    """Return the id of the bot's comment on the PR, or None. Paginates."""
    page = 1
    while True:
        url = (f"{API}/repos/{repo}/issues/{pr}/comments"
               f"?per_page=100&page={page}")
        batch = _req("GET", url, token)
        if not batch:
            return None
        for c in batch:
            if marker in (c.get("body") or ""):
                return c["id"]
        if len(batch) < 100:
            return None
        page += 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--body", required=True, help="path to markdown body")
    ap.add_argument("--marker", default=DEFAULT_MARKER)
    ap.add_argument("--repo", default=os.environ.get("GH_REPO")
                    or os.environ.get("GITHUB_REPOSITORY"))
    opts = ap.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.stderr.write("error: GITHUB_TOKEN not set\n")
        return 2
    if not opts.repo:
        sys.stderr.write("error: repo not set (GH_REPO/GITHUB_REPOSITORY)\n")
        return 2

    body = open(opts.body, encoding="utf-8").read()
    if opts.marker not in body:
        # Guarantee future upserts can find this comment.
        body = opts.marker + "\n" + body

    try:
        existing = find_existing(opts.repo, opts.pr, token, opts.marker)
        if existing is not None:
            _req("PATCH",
                 f"{API}/repos/{opts.repo}/issues/comments/{existing}",
                 token, {"body": body})
            print(f"updated comment {existing} on PR #{opts.pr}")
        else:
            created = _req("POST",
                           f"{API}/repos/{opts.repo}/issues/{opts.pr}/comments",
                           token, {"body": body})
            cid = created.get("id") if isinstance(created, dict) else None
            print(f"created comment {cid} on PR #{opts.pr}")
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"GitHub API error {e.code}: {e.read().decode()}\n")
        return 1
    except urllib.error.URLError as e:  # network/DNS/TLS/timeout
        sys.stderr.write(f"GitHub API unreachable: {e.reason}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
