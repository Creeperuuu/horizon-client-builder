#!/usr/bin/env python3
"""Validate, unpack and (optionally) re-pin source-based Horizon .hnmod archives.

The builder site uploads .hnmod files to a temporary Git branch; this script
turns them into the directory layout the horizon-mixins `apply` tool expects:

    <output-dir>/<mod-id>/horizon-mixin.toml
    <output-dir>/<mod-id>/<...mod sources...>

With --force-revision <sha>, every staged mod's `[compatibility].horizon_revision`
is rewritten to <sha>. scripts/probe_revisions.sh uses that per candidate
revision while probing: the build lands on the first Horizon commit where the
mixin tooling still resolves every selected mod's source anchors, which is
what lets mods written against different Horizon commits build together.

With --list-pins, prints the distinct, validated pins of the staged mods as
one compact JSON line ({"pins": [...], "count": N}) for the build workflow.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

MAX_ARCHIVE = 40 * 1024 * 1024
MAX_UNPACKED = 200 * 1024 * 1024
MAX_FILES = 10
REVISION_RE = re.compile(r'[0-9a-fA-F]{40}')


def checked_path(name: str) -> PurePosixPath:
    """Rejects absolute paths, drive letters, backslashes and .. traversal."""
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(p in ("", ".", "..") for p in path.parts):
        raise ValueError(f"Unsafe path in archive: {name!r}")
    if "\\" in name or ":" in name:
        raise ValueError(f"Unsafe path in archive: {name!r}")
    return path


def rewrite_revision(manifest: Path, revision: str) -> str | None:
    """Rewrites [compatibility].horizon_revision to `revision`.

    Returns the previous pin (or None). The rewritten file is re-parsed with
    tomllib so a botched edit cannot reach the build.
    """
    text = manifest.read_text(encoding="utf-8")
    if not REVISION_RE.fullmatch(revision):
        raise ValueError(f"--force-revision must be a 40-hex commit SHA, got {revision!r}")

    previous = None
    pattern = re.compile(r'(?m)^(horizon_revision\s*=\s*)"[^"]*"')
    if pattern.search(text):
        old = re.search(r'(?m)^horizon_revision\s*=\s*"([^"]*)"', text)
        previous = old.group(1) if old else None
        text = pattern.sub(lambda m: f'{m.group(1)}"{revision}"', text)
    elif re.search(r'(?m)^\[compatibility\]\s*$', text):
        text = re.sub(r'(?m)^(\[compatibility\]\s*)$',
                      rf'\g<1>horizon_revision = "{revision}"\n', text)
    else:
        text = text.rstrip("\n") + f'\n\n[compatibility]\nhorizon_revision = "{revision}"\n'

    manifest.write_text(text, encoding="utf-8")
    parsed = tomllib.loads(manifest.read_text(encoding="utf-8"))
    written = parsed.get("compatibility", {}).get("horizon_revision")
    if written != revision:
        raise ValueError(f"Revision rewrite failed for {manifest.name}: {written!r}")
    return previous


def stage(archive: Path, output: Path, seen: dict[str, str]) -> dict:
    if archive.suffix.lower() != ".hnmod":
        raise ValueError(f"Not a .hnmod archive: {archive.name}")
    if archive.stat().st_size > MAX_ARCHIVE:
        raise ValueError(f"{archive.name} exceeds the 40 MiB archive limit")
    with zipfile.ZipFile(archive) as zf:
        files: dict[str, zipfile.ZipInfo] = {}
        total = 0
        for info in zf.infolist():
            if info.is_dir():
                continue
            path = checked_path(info.filename)
            total += info.file_size
            if total > MAX_UNPACKED:
                raise ValueError(f"{archive.name} expands beyond 200 MiB")
            if path.as_posix() in files:
                raise ValueError(f"Duplicate archive member: {path}")
            files[path.as_posix()] = info

        manifest_names = [n for n in files if n.endswith("/horizon-mixin.toml")]
        if len(manifest_names) != 1:
            raise ValueError(f"{archive.name}: expected exactly one mixins/<id>/horizon-mixin.toml")
        manifest_path = PurePosixPath(manifest_names[0])
        if len(manifest_path.parts) != 3 or manifest_path.parts[0] != "mixins":
            raise ValueError(f"{archive.name}: mixin manifest must be in mixins/<id>/")
        folder_id = manifest_path.parts[1]
        manifest = tomllib.loads(zf.read(files[manifest_names[0]]).decode("utf-8"))
        mod = manifest.get("mod", {})
        mod_id = mod.get("id")
        if not isinstance(mod_id, str) or mod_id != folder_id:
            raise ValueError(f"{archive.name}: manifest mod.id must match folder {folder_id!r}")
        if not mod_id or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in mod_id):
            raise ValueError(f"{archive.name}: invalid native mod ID {mod_id!r}")
        if mod_id in seen:
            raise ValueError(
                f"Duplicate native mod ID {mod_id!r} (also in {seen[mod_id]}); remove one of the mods")
        if "horizon-native.json" in files:
            native = json.loads(zf.read(files["horizon-native.json"]).decode("utf-8"))
            if native.get("id") != mod_id:
                raise ValueError(f"{archive.name}: horizon-native.json ID does not match {mod_id!r}")

        destination = output / mod_id
        destination.mkdir(parents=True, exist_ok=False)
        prefix = ("mixins", mod_id)
        count = 0
        for name, info in files.items():
            member = PurePosixPath(name)
            if member.parts[:2] != prefix or len(member.parts) <= 2:
                continue
            target = destination.joinpath(*member.parts[2:])
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as source, target.open("wb") as dest:
                shutil.copyfileobj(source, dest)
            count += 1
        if not (destination / "horizon-mixin.toml").is_file():
            raise ValueError(f"{archive.name}: mixin manifest was not staged")
        seen[mod_id] = archive.name
        return {
            "file": archive.name,
            "id": mod_id,
            "version": str(mod.get("version", "unknown")),
            "pinned_revision": manifest.get("compatibility", {}).get("horizon_revision"),
            "files": count,
        }


def list_pins(output: Path) -> int:
    """Prints the distinct validated Horizon pins of the staged mods."""
    pins: set[str] = set()
    for manifest in sorted(output.glob("*/horizon-mixin.toml")):
        try:
            parsed = tomllib.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            print(f"Could not read {manifest}: {exc}", file=sys.stderr)
            return 2
        revision = parsed.get("compatibility", {}).get("horizon_revision")
        if revision is None:
            continue
        if not isinstance(revision, str) or not REVISION_RE.fullmatch(revision):
            print(f"Invalid [compatibility].horizon_revision in {manifest}: {revision!r}",
                  file=sys.stderr)
            return 2
        pins.add(revision.lower())
    print(json.dumps({"pins": sorted(pins), "count": len(pins)},
                     separators=(",", ":")))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=False,
                        help="folder containing the uploaded .hnmod archives")
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="folder to stage unpacked mods into (the apply --mods directory)")
    parser.add_argument("--force-revision", metavar="SHA", default=None,
                        help="rewrite every mod's [compatibility].horizon_revision to this commit")
    parser.add_argument("--rewrite-staged", action="store_true",
                        help="skip unpacking; rewrite mods already staged in --output-dir")
    parser.add_argument("--list-pins", action="store_true",
                        help="print the staged mods' distinct Horizon pins as JSON and exit")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    if args.list_pins:
        return list_pins(args.output_dir)

    if args.rewrite_staged:
        manifests = sorted(args.output_dir.glob("*/horizon-mixin.toml"))
        if not manifests:
            print("--rewrite-staged found no staged mods", file=sys.stderr)
            return 2
        if not args.force_revision:
            print("--rewrite-staged requires --force-revision", file=sys.stderr)
            return 2
        results = []
        try:
            for manifest in manifests:
                previous = rewrite_revision(manifest, args.force_revision)
                results.append({"id": manifest.parent.name,
                                "revision_override": {"from": previous,
                                                       "to": args.force_revision}})
        except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
            print(f"Revision rewrite failed: {exc}", file=sys.stderr)
            return 3
        print(json.dumps({"count": len(results), "force_revision": args.force_revision,
                          "mods": results}, indent=2))
        return 0

    if args.input_dir is None:
        parser.error("--input-dir is required unless --rewrite-staged is used")
    archives = sorted(args.input_dir.glob("*.hnmod")) if args.input_dir.exists() else []
    if not archives:
        print(json.dumps({"count": 0, "mods": [], "note": "clean build without mods"}))
        return 0
    if len(archives) > MAX_FILES:
        print(f"Too many .hnmod files ({len(archives)}); the limit is {MAX_FILES}.", file=sys.stderr)
        return 2

    seen: dict[str, str] = {}
    results = []
    try:
        for archive in archives:
            results.append(stage(archive, args.output_dir, seen))
    except (OSError, ValueError, KeyError, UnicodeDecodeError, zipfile.BadZipFile,
            json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        print(f"Native mod validation failed: {exc}", file=sys.stderr)
        return 2

    if args.force_revision:
        for result in results:
            manifest = args.output_dir / result["id"] / "horizon-mixin.toml"
            try:
                previous = rewrite_revision(manifest, args.force_revision)
            except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
                print(f"Revision rewrite failed: {exc}", file=sys.stderr)
                return 3
            result["revision_override"] = {
                "from": previous, "to": args.force_revision,
            }

    print(json.dumps({"count": len(results), "force_revision": args.force_revision,
                      "mods": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
