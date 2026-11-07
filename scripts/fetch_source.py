#!/usr/bin/env python3
"""Horizon's source at one exact commit, fetched the way the Horizon launcher does it.

This is launcher/src/native_mods.rs `source()` in Python: GitHub's archive of the commit
(codeload.github.com/<repo>/tar.gz/<commit>), unpacked with links left out, the archive's one top
folder as the root, and the commit written to `.horizon-revision`.

Every file is written with the current time, not the commit's. A restored Cargo cache holds
crates compiled from other commits; a source file dated by its commit can look older than that
build, and Cargo would then link the stale crate (Horizon's #10421, see `sync` in the launcher).

usage: fetch_source.py <commit> <out dir>
"""
import os
import pathlib
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.request

UPSTREAM = os.environ.get("UPSTREAM", "horizon-rust/horizon")


def download(url, to, attempts=4):
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "horizon-client-builder"})
            with urllib.request.urlopen(request, timeout=120) as response, open(to, "wb") as out:
                shutil.copyfileobj(response, out)
            return
        except OSError as error:
            if attempt == attempts:
                sys.exit(f"::error::could not download {url}: {error}")
            print(f"download failed ({error}); retrying", flush=True)
            time.sleep(5 * attempt)


def unpack(archive, out):
    files = 0
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            # Links are left out: none is needed to build (CLAUDE.md -> AGENTS.md is one).
            if not (member.isfile() or member.isdir()):
                continue
            parts = pathlib.PurePosixPath(member.name).parts[1:]  # drop <repo>-<commit>/
            if not parts or ".." in parts or pathlib.PurePosixPath(member.name).is_absolute():
                continue
            target = out.joinpath(*parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as source, open(target, "wb") as dest:
                shutil.copyfileobj(source, dest)
            if member.mode & 0o111:
                target.chmod(0o755)
            files += 1
    return files


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    commit, out = sys.argv[1], pathlib.Path(sys.argv[2])
    if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        sys.exit(f"::error::{commit!r} is not a full commit hash")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as scratch:
        archive = pathlib.Path(scratch) / "source.tar.gz"
        download(f"https://codeload.github.com/{UPSTREAM}/tar.gz/{commit}", archive)
        files = unpack(archive, out)
    (out / ".horizon-revision").write_text(commit, encoding="utf-8")
    if not (out / "Cargo.lock").is_file() or not (out / "crates" / "horizon-client").is_dir():
        sys.exit("::error::the archive is not Horizon's source (no Cargo.lock or horizon-client)")
    print(f"Horizon {commit[:7]}: {files} files in {out}")


if __name__ == "__main__":
    main()
