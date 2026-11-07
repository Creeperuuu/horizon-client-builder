#!/usr/bin/env python3
"""Resolve a Horizon revision to one full commit, the way Horizon itself pins builds.

A revision is one of:
  nightly              the newest Nightly: the newest `nightly-source/<date>-<short>` tag
                       (Horizon keeps those tags for good, so any Nightly stays buildable)
  nightly-source/...   one specific Nightly (or just `2026-11-05-f319701`)
  v2026.10.23          a release tag
  main / any branch    that branch's tip right now
  <7-40 hex chars>     an exact commit

It prints what it found and, under GitHub Actions, writes these step outputs:
  sha, short, kind, label, version, nightly

`version` and `nightly` are what Horizon's own CI passes to the build as HORIZON_VERSION and
HORIZON_NIGHTLY (crates/horizon-core/src/release.rs). They are only set for a Nightly or a
release tag; a branch or a commit builds as "a build from source", exactly like a local build.
"""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

UPSTREAM = os.environ.get("UPSTREAM", "horizon-rust/horizon")
GIT = f"https://github.com/{UPSTREAM}"
NIGHTLY_TAG = re.compile(r"^nightly-source/(\d{4}-\d{2}-\d{2})-([0-9a-f]{7,40})$")
RELEASE_TAG = re.compile(r"^v?(\d+(?:\.\d+)+)$")


def die(message):
    print(f"::error::{message}")
    sys.exit(1)


def ls_remote(*patterns):
    """ref -> commit for the refs matching `patterns` (an annotated tag's peeled commit wins)."""
    out = subprocess.run(
        ["git", "ls-remote", GIT, *patterns], check=True, capture_output=True, text=True
    ).stdout
    refs = {}
    for line in out.splitlines():
        sha, ref = line.split("\t")
        peeled = ref.endswith("^{}")
        ref = ref.removesuffix("^{}")
        if peeled or ref not in refs:
            refs[ref] = sha
    return refs


def api(path):
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "horizon-client-builder"},
    )
    if os.environ.get("GH_TOKEN"):
        request.add_header("Authorization", f"Bearer {os.environ['GH_TOKEN']}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        die(f"GitHub answered {error.code} for {path}: {error.reason}")


def newest_nightly():
    """The newest Nightly's tag. Several can share a date, and their short hashes don't sort by
    time, so the day's candidates are ordered by their commits' dates."""
    tags = {}
    for ref, sha in ls_remote("refs/tags/nightly-source/*").items():
        name = ref.removeprefix("refs/tags/")
        match = NIGHTLY_TAG.match(name)
        if match:
            tags[name] = (match.group(1), sha)
    if not tags:
        die(f"{UPSTREAM} has no nightly-source/* tags")
    day = max(date for date, _ in tags.values())
    todays = [(name, sha) for name, (date, sha) in tags.items() if date == day]

    def committed(item):
        return api(f"/repos/{UPSTREAM}/commits/{item[1]}")["commit"]["committer"]["date"]

    return max(todays, key=committed) if len(todays) > 1 else todays[0]


def resolve(revision):
    """-> (kind, name, full sha)"""
    revision = revision.strip() or "nightly"
    if revision in ("nightly", "latest-nightly"):
        name, sha = newest_nightly()
        return "nightly", name, sha
    if re.fullmatch(r"[0-9a-fA-F]{7,40}", revision):
        return "commit", revision, api(f"/repos/{UPSTREAM}/commits/{revision}")["sha"]
    name = revision.removeprefix("refs/tags/").removeprefix("refs/heads/")
    refs = ls_remote(f"refs/tags/{name}", f"refs/heads/{name}", f"refs/tags/nightly-source/{name}")
    for ref, kind in (
        (f"refs/tags/{name}", "tag"),
        (f"refs/tags/nightly-source/{name}", "tag"),
        (f"refs/heads/{name}", "branch"),
    ):
        if ref in refs:
            tag = ref.removeprefix("refs/tags/")
            return ("nightly" if NIGHTLY_TAG.match(tag) else kind), tag if kind == "tag" else name, refs[ref]
    die(f"{UPSTREAM} has no tag, branch or commit called {revision!r}")


def main():
    kind, name, sha = resolve(sys.argv[1] if len(sys.argv) > 1 else "nightly")
    short = sha[:7]
    version = nightly = ""
    if kind == "nightly":
        date = NIGHTLY_TAG.match(name).group(1)
        version, nightly = f"nightly-{date}", f"{date} {short}"
        label = f"Nightly {date} · {short}"
    elif kind == "tag":
        release = RELEASE_TAG.match(name)
        version = release.group(1) if release else ""
        label = f"{name} · {short}"
    elif kind == "branch":
        label = f"{name} @ {short}"
    else:
        label = f"Commit {short}"

    print(f"revision  {name}\nkind      {kind}\ncommit    {sha}\nlabel     {label}")
    print(f"version   {version or '(build from source)'}\nnightly   {nightly or '(none)'}")

    outputs = dict(sha=sha, short=short, kind=kind, label=label, version=version, nightly=nightly)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            for key, value in outputs.items():
                out.write(f"{key}={value}\n")


if __name__ == "__main__":
    main()
