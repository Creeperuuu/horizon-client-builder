#!/usr/bin/env python3
"""Resolve a Horizon revision to one full commit, the way Horizon itself pins builds.

A revision is one of:
  nightly              the newest Nightly: the newest `nightly-source/<date>-<short>` tag
                       (Horizon keeps those tags for good, so any Nightly stays buildable)
  nightly-source/...   one specific Nightly (or just `2026-11-05-f319701`)
  v2026.10.23          a release tag
  main / any branch    that branch's tip right now
  <7-40 hex chars>     an exact commit

With native mods (mods/, see stage_mods.py) it also decides which commit to build, the way the Horizon
launcher does (launcher/src/native_mods.rs):
  - mods pin the Horizon commit they were made for (`[compatibility] horizon_revision`). A pin chooses the
    commit even when another revision was asked for; the revision asked for is used only when no mod pins
    one, or with --pins ignore ("Rebuild on Horizon": every mod is moved to it)
  - mods pinning different commits build on the newest of them, but only with --allow-mismatch
  - a mod that names a hook points' version (`hooks`) builds on any commit whose NATIVE_HOOKS_API is that
    or later, and finds the named hook points in that commit's native-hooks.json
  - horizon-mixins runs at the newest commit the mods name (`mixins`), else the launcher's own MIXINS_REV
    at the built commit; Rust is the launcher's pinned version at the built commit

It prints what it found and, under GitHub Actions, writes these step outputs:
  sha, short, kind, label, version, nightly, mixins, rust, mods, mods_count, mods_digest, notes

`version` and `nightly` are what Horizon's own CI passes to the build as HORIZON_VERSION and
HORIZON_NIGHTLY (crates/horizon-core/src/release.rs). They are only set for a Nightly or a
release tag; a branch or a commit builds as "a build from source", exactly like a local build.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stage_mods  # noqa: E402

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


def raw(commit, path, repo=None):
    """A file of `repo` (default: Horizon) at `commit`, or None."""
    url = f"https://raw.githubusercontent.com/{repo or UPSTREAM}/{commit}/{path}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "horizon-client-builder"}), timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError:
        return None


def commit_date(commit, repo=None):
    return api(f"/repos/{repo or UPSTREAM}/commits/{commit}")["commit"]["committer"]["date"]


def newest(commits, repo=None):
    """The newest of `commits` by when each was made, as the launcher's `newest` does."""
    return max(commits, key=lambda c: commit_date(c, repo))


def hooks_api(commit):
    """The native hook points' version of Horizon at `commit` (None: it has none)."""
    text = raw(commit, "crates/horizon-client/src/game/native_hooks.rs") or ""
    found = re.search(r"pub\(crate\) const NATIVE_HOOKS_API: u32 = (\d+);", text)
    return int(found.group(1)) if found else None


def hook_catalog(commit):
    """{(name, version)} of the named hook points in native-hooks.json at `commit` (None: no catalog)."""
    text = raw(commit, "native-hooks.json")
    try:
        return {(h["name"], int(h["version"])) for h in json.loads(text)["hooks"]} if text else None
    except (ValueError, KeyError, TypeError):
        return None


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


def fits(bundle, commit):
    """Whether a hooks-version mod finds its hook points at `commit` (launcher's `with_hooks`)."""
    found = hooks_api(commit)
    if found is None or bundle.hooks > found:
        return False
    named = bundle.named_hooks()
    if not named:
        return True
    catalog = hook_catalog(commit)
    return catalog is not None and all(pair in catalog for pair in named)


def choose_commit(base, bundles, pins, allow_mismatch):
    """The commit the mods build on. -> (commit, why)"""
    if not bundles:
        return base, "no mods"
    free = [b for b in bundles if b.hooks is None]
    hooked = [b for b in bundles if b.hooks is not None]
    distinct = sorted({b.horizon for b in free if b.horizon})
    if pins == "ignore":
        pinned, why = base, "the mods' pins are ignored: every mod is moved to the revision asked for"
    elif not distinct:
        pinned, why = base, "no mod pins a commit: the revision asked for"
    elif len(distinct) == 1:
        pinned, why = distinct[0], "the commit the mods pin"
    else:
        who = "; ".join(f"{c[:7]}: {', '.join(b.id for b in free if b.horizon == c)}" for c in distinct)
        latest = newest(distinct)
        if not allow_mismatch:
            die(f"The mods pin different Horizon commits ({who}). They would build on the newest, {latest[:7]}, "
                "where an older mod may not apply or compile. Run again with allow_mismatch to build anyway, "
                "or pins=ignore to build every mod on the revision you asked for.")
        pinned, why = latest, f"the newest of the commits the mods pin ({who})"
    if not hooked or all(fits(b, pinned) for b in hooked):
        return pinned, why
    # A hooks-version mod's own fallbacks, in the launcher's order: the commit it was tested on (only when
    # nothing pins), then the revision asked for.
    tested = sorted({b.horizon for b in hooked if b.horizon})
    if not distinct and len(tested) == 1 and all(fits(b, tested[0]) for b in hooked):
        return tested[0], "the commit the hook-point mods were tested on"
    if pinned != base and all(fits(b, base) for b in hooked):
        return base, "the revision asked for: the pinned commit lacks hook points a mod needs"
    lacking = ", ".join(f"{b.id} (hook points v{b.hooks})" for b in hooked if not fits(b, pinned))
    die(f"Horizon {pinned[:7]} doesn't have the native hook points these mods use: {lacking}. Pick a newer revision or update the mods.")


def choose_mixins(bundles, commit):
    pins = sorted({b.mixins for b in bundles if b.mixins})
    text = raw(commit, "launcher/src/native_mods.rs") or ""
    found = re.search(r'pub const MIXINS_REV: &str = "([0-9a-f]{40})"', text)
    own = found.group(1) if found else None
    if len(pins) == 1:
        return pins[0]
    if not pins:
        if not own:
            die(f"Horizon {commit[:7]} doesn't say which horizon-mixins it uses (no MIXINS_REV), and no mod names one")
        return own
    return newest(pins + ([own] if own and own not in pins else []), "horizon-rust/horizon-mixins")


def rust_version(commit):
    found = re.search(r'pub const VERSION: &str = "([^"]+)"', raw(commit, "launcher/src/rust_toolchain.rs") or "")
    return found.group(1) if found else "stable"


def describe(commit, base):
    """(kind, name) for `commit`: what it was asked as, else a Nightly or release tag that points at it."""
    if commit == base[2]:
        return base[0], base[1]
    for ref, sha in ls_remote("refs/tags/nightly-source/*", "refs/tags/v*").items():
        tag = ref.removeprefix("refs/tags/")
        if sha == commit:
            return ("nightly" if NIGHTLY_TAG.match(tag) else "tag"), tag
    return "commit", commit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("revision", nargs="?", default="nightly")
    parser.add_argument("--mods", type=Path, default=Path("mods"))
    parser.add_argument("--select", default="all")
    parser.add_argument("--pins", choices=["follow", "ignore"], default="follow")
    parser.add_argument("--allow-mismatch", default="false")
    args = parser.parse_args()

    base = resolve(args.revision)
    try:
        bundles = stage_mods.discover(args.mods, args.select)
    except (stage_mods.ModError, ValueError, OSError) as error:
        die(str(error))
    sha, why = choose_commit(base[2], bundles, args.pins, args.allow_mismatch.lower() == "true")
    kind, name = describe(sha, base)
    mixins = choose_mixins(bundles, sha) if bundles else ""
    rust = rust_version(sha) if bundles else "stable"
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

    print(f"revision  {args.revision} -> {name}\nkind      {kind}\ncommit    {sha}\nlabel     {label}")
    print(f"version   {version or '(build from source)'}\nnightly   {nightly or '(none)'}")

    if bundles:
        print(f"\nbuilt on  {short}: {why}")
        print(f"mixins    {mixins}\nrust      {rust}")
        for b in bundles:
            moved = "" if not b.horizon or b.horizon == sha else f"  (pinned {b.horizon[:7]}, moved to {short})"
            print(f"mod       {b.id} {b.version}  {b.source}{moved}")
    notes = why if bundles else ""
    outputs = dict(sha=sha, short=short, kind=kind, label=label, version=version, nightly=nightly,
                   mixins=mixins, rust=rust, mods_count=str(len(bundles)), mods_digest=stage_mods.digest(bundles),
                   mods=json.dumps([b.summary() for b in bundles], separators=(",", ":")), notes=notes)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            for key, value in outputs.items():
                out.write(f"{key}={value}\n")


if __name__ == "__main__":
    main()
