#!/usr/bin/env python3
"""Find, check and stage Horizon native mods (.hnmod) for horizon-mixins.

What this does is what the Horizon launcher does before it compiles native mods
(docs/native-mods.md in horizon-rust/horizon, launcher/src/native_mods.rs):

  * A mod is a `.hnmod` (a zip: `horizon-native.json` and `mixins/<id>/...`), an unpacked one, or a
    bare bundle folder holding `horizon-mixin.toml`. Reading one never runs its code. A `.hnmod` is
    checked by the rules of horizon_mods::index::native::Bundle::read: no links, no `..`, only Rust,
    TOML and `data/` files under `mixins/<id>/`, at most 512 entries and 64 MiB, and the manifest
    and the mixin file must agree on id, version and Horizon revision.
  * Each mod's `[compatibility] horizon_revision` is rewritten to the commit that is built
    (`repin`) when it names another, and launcher-only `[dependencies]` is dropped (`api::strip`).
    This works on a copy: the files in mods/ are never changed.

Not supported here, said plainly instead of building something wrong: mods with an `[api]` crate or
`trait_method` injections (the launcher generates an extra game package for those) and mods with
`[[post_build]]` steps (the launcher runs those inside an installed version folder, which a lone
horizon-client.exe doesn't have).

usage:
  stage_mods.py list  --mods DIR [--select all|none|a,b]
  stage_mods.py stage --mods DIR [--select ...] --commit SHA --out DIR
"""
import argparse
import hashlib
import json
import re
import shutil
import sys
import tomllib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

MANIFEST = "horizon-native.json"  # a .hnmod's own manifest
MIXIN = "horizon-mixin.toml"
MAX_BYTES = 64 << 20
MAX_ENTRIES = 512
HEX40 = re.compile(r"[0-9a-f]{40}")
SKIP_DIRS = {".git", "target", "node_modules", "__pycache__"}


class ModError(Exception):
    pass


@dataclass
class Bundle:
    id: str
    version: str
    source: str  # the file or folder name in mods/
    horizon: str | None  # [compatibility] horizon_revision
    mixins: str | None  # the horizon-mixins commit it was tested with (.hnmod only)
    hooks: int | None  # the native hook points' version it needs (.hnmod only)
    toml: dict
    sha256: str
    files: dict = field(default_factory=dict, repr=False)  # path under the bundle -> bytes

    def named_hooks(self):
        """(name, hook_version) of each injection that names a hook point."""
        return [
            (i["hook"], i["hook_version"])
            for i in self.toml.get("injection", [])
            if isinstance(i, dict) and "hook" in i and isinstance(i.get("hook_version"), int)
        ]

    def summary(self):
        return dict(id=self.id, version=self.version, file=self.source, sha256=self.sha256[:16],
                    horizon=self.horizon, mixins=self.mixins, hooks=self.hooks)


def valid_id(value):
    """horizon_mods::index::valid_id: 1 to 64 of a-z, 0-9 and _."""
    return bool(re.fullmatch(r"[a-z0-9_]{1,64}", value or ""))


def is_revision(value):
    return bool(value) and bool(HEX40.fullmatch(value))


# What `Bundle::read` allows beside the manifest, under mixins/<id>/ (`allowed`, `in_data`, `text`).
def allowed(rest):
    parts = rest.split("/")
    in_data = len(parts) >= 2 and (parts[0] == "data" or (len(parts) >= 3 and parts[1] == "data"))
    name = parts[-1]
    text = len(parts) <= 2 and (
        any(name.startswith(p) for p in ("README", "LICENSE", "LICENCE", "COPYING", "NOTICE"))
        or name == ".gitignore"
    )
    return rest.endswith(".rs") or rest.endswith(".toml") or in_data or text


def unsupported(bundle):
    """Why this mod can't be built into a lone horizon-client.exe here, or None."""
    t = bundle.toml
    if "api" in t:
        return "has an [api] trait crate; the launcher builds those with an extra generated game package, which this builder doesn't do yet"
    if any(isinstance(i, dict) and "trait_method" in i for i in t.get("injection", [])):
        return "uses trait_method injections, which need an [api] crate this builder doesn't build yet"
    if "post_build" in t:
        return "has [[post_build]] steps, which the launcher runs inside an installed Horizon version folder; a standalone horizon-client.exe has none"
    if not any(k not in ("mod", "compatibility", "api", "dependencies") for k in t):
        return "has no mixin content (injections or crates) for horizon-mixins to apply"
    return None


def _parse_mixin(text, where):
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ModError(f"{where}: {MIXIN} is not valid TOML: {error}") from None


def _bundle(toml, source, files, native=None):
    mod = toml.get("mod") or {}
    ident, version = mod.get("id"), mod.get("version")
    if not valid_id(ident):
        raise ModError(f"{source}: invalid mod id {ident!r} (1 to 64 of a-z, 0-9 and _)")
    if not isinstance(version, str) or not version:
        raise ModError(f"{source}: [mod] needs a version")
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode() + b"\0" + hashlib.sha256(files[name]).digest())
    native = native or {}
    return Bundle(
        id=ident, version=version, source=source,
        horizon=(toml.get("compatibility") or {}).get("horizon_revision") or native.get("horizon"),
        mixins=native.get("mixins"), hooks=native.get("hooks"),
        toml=toml, sha256=digest.hexdigest(), files=files,
    )


def read_hnmod(path):
    """A `.hnmod` (or a `.hmod` that holds native code), by horizon_mods' Bundle::read."""
    name = path.name
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise ModError(f"{name}: not a zip file") from None
    files, total = {}, 0
    with zf:
        for info in zf.infolist():
            entry = info.filename
            if info.is_dir():
                continue
            mode = (info.external_attr >> 16) & 0o170000
            if (mode and mode != 0o100000) or "\\" in entry or ":" in entry or entry.startswith("/") \
                    or any(p in ("", ".", "..") for p in entry.split("/")):
                raise ModError(f"{name}: invalid native bundle path: {entry}")
            if entry == "mod.wasm":
                raise ModError(f"{name}: WebAssembly mods don't run any more; port the code to a native mod")
            top = PurePosixPath(entry).parts[0]
            if entry == "horizon-mod.toml" or top in ("assets", "data"):
                continue  # a .hmod's content half: not part of the native build
            if len(files) >= MAX_ENTRIES:
                raise ModError(f"{name}: native bundle has too many entries")
            data = zf.read(info)
            total += len(data)
            if total > MAX_BYTES:
                raise ModError(f"{name}: native bundle expands beyond 64 MiB")
            if entry in files:
                raise ModError(f"{name}: duplicate native bundle path {entry}")
            files[entry] = data
    if MANIFEST not in files:
        raise ModError(f"{name}: missing {MANIFEST}")
    try:
        native = json.loads(files.pop(MANIFEST))
    except ValueError as error:
        raise ModError(f"{name}: {MANIFEST}: {error}") from None
    known = {"schema", "id", "version", "game", "horizon", "mixins", "hooks"}
    if set(native) - known:
        raise ModError(f"{name}: {MANIFEST} has unknown fields {sorted(set(native) - known)}")
    if native.get("schema") != 1 or not valid_id(native.get("id")) or not native.get("version") \
            or not native.get("game") or not is_revision(native.get("horizon")) \
            or not is_revision(native.get("mixins")) or native.get("hooks") == 0:
        raise ModError(f"{name}: invalid native manifest: schema 1, id, version, game and exact revisions required")
    prefix = f"mixins/{native['id']}/"
    bundle_files = {}
    for entry, data in files.items():
        rest = entry.removeprefix(prefix)
        if rest == entry or not allowed(rest):
            raise ModError(f"{name}: unexpected native source: {entry}")
        bundle_files[rest] = data
    if MIXIN not in bundle_files:
        raise ModError(f"{name}: missing {prefix}{MIXIN}")
    toml = _parse_mixin(bundle_files[MIXIN].decode("utf-8", "replace"), name)
    mod, compat = toml.get("mod") or {}, toml.get("compatibility") or {}
    if mod.get("id") != native["id"] or mod.get("version") != native["version"] \
            or compat.get("horizon_revision") != native["horizon"]:
        raise ModError(f"{name}: native manifest and mixin identity/revision differ")
    return _bundle(toml, name, bundle_files, native)


def read_folder(directory):
    """A bundle folder (`horizon-mixin.toml`) or an unpacked `.hnmod` (`horizon-native.json`)."""
    native = None
    root = directory
    if not (directory / MIXIN).is_file():
        try:
            native = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
            root = directory / "mixins" / native["id"]
        except (OSError, ValueError, KeyError) as error:
            raise ModError(f"{directory.name}: unreadable {MANIFEST}: {error}") from None
    elif (directory / MANIFEST).is_file():
        try:
            native = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
        except ValueError as error:
            raise ModError(f"{directory.name}: unreadable {MANIFEST}: {error}") from None
    if not (root / MIXIN).is_file():
        raise ModError(f"{directory.name}: no {MIXIN}")
    files, total = {}, 0
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(p in SKIP_DIRS for p in rel.parts):
            continue
        if path.is_symlink():
            raise ModError(f"{directory.name}: links are refused ({rel.as_posix()})")
        if path.is_file():
            total += path.stat().st_size
            if total > MAX_BYTES or len(files) >= 4096:
                raise ModError(f"{directory.name}: bundle is too large")
            files[rel.as_posix()] = path.read_bytes()
    toml = _parse_mixin(files[MIXIN].decode("utf-8", "replace"), directory.name)
    return _bundle(toml, directory.name, files, native)


def _is_bundle_dir(path):
    return (path / MIXIN).is_file() or (path / MANIFEST).is_file()


def discover(mods_dir, select="all"):
    """The mods in `mods_dir`, narrowed by `select` (`all`, `none`, or names, comma-separated:
    a file or folder name, with or without `.hnmod`, or a mod id).

    A mod that can't be read stops only the builds that include it: leave it out with `select` and the
    others still build."""
    select = (select or "all").strip()
    if select.lower() == "none":
        return []
    found, broken = [], {}  # broken: entry name -> why

    def attempt(entry, read):
        try:
            found.append(read(entry))
        except (ModError, ValueError, OSError, zipfile.BadZipFile) as error:
            broken[entry.name] = str(error) if isinstance(error, ModError) else f"{entry.name}: {error}"

    if mods_dir.is_dir():
        for entry in sorted(mods_dir.iterdir()):
            if entry.name.startswith("."):
                continue
            if entry.is_file() and entry.suffix.lower() in (".hnmod", ".hmod"):
                try:
                    with zipfile.ZipFile(entry) as zf:
                        native = MANIFEST in zf.namelist()
                except zipfile.BadZipFile:
                    broken[entry.name] = f"{entry.name}: not a zip file"
                    continue
                if not native:
                    print(f"::notice::{entry.name} is a content mod (no native code): it can't be built into the exe, skipped")
                    continue
                attempt(entry, read_hnmod)
            elif entry.is_dir():
                if _is_bundle_dir(entry):
                    attempt(entry, read_folder)
                else:
                    for sub in sorted(entry.iterdir()):
                        if sub.is_dir() and _is_bundle_dir(sub):
                            attempt(sub, read_folder)
    if select.lower() != "all":
        wanted = [w.strip() for w in select.split(",") if w.strip()]
        keep = []
        for want in wanted:
            hit = [b for b in found if want in (b.source, Path(b.source).stem, b.id)]
            if not hit:
                for name, why in broken.items():
                    if want in (name, Path(name).stem):
                        raise ModError(why)
                raise ModError(f"No mod called {want!r} in mods/ (it has: {', '.join(b.source for b in found) or 'nothing'})")
            keep += [b for b in hit if b not in keep]
        found = keep
    elif broken:
        name, why = next(iter(broken.items()))
        extra = f" (and {len(broken) - 1} more)" if len(broken) > 1 else ""
        raise ModError(f"{why}{extra}. Fix or remove it, or build without it by naming the mods you want.")
    ids = {}
    for bundle in found:  # only the mods being built must have distinct ids
        if bundle.id in ids:
            raise ModError(f"Two mods are called {bundle.id!r} ({ids[bundle.id]} and {bundle.source}); remove one or build only one")
        ids[bundle.id] = bundle.source
    for bundle in found:
        why = unsupported(bundle)
        if why:
            raise ModError(f"{bundle.source} ({bundle.id}) {why}.")
    return found


def digest(bundles):
    """Eight hex digits naming this set of mods (and their exact files), '' for none."""
    if not bundles:
        return ""
    text = "|".join(sorted(f"{b.id}@{b.sha256}" for b in bundles))
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def _drop_table(text, name):
    """`text` without the `[name]` table (and `[name.x]` subtables)."""
    out, skipping = [], False
    for line in text.splitlines(keepends=True):
        header = re.match(r"\s*\[\[?\s*([A-Za-z0-9_.\"-]+)\s*\]\]?", line)
        if header:
            key = header.group(1)
            skipping = key == name or key.startswith(name + ".")
        if not skipping:
            out.append(line)
    return "".join(out)


def _repin(text, commit):
    """`text` with `[compatibility] horizon_revision` set to `commit` (the launcher's `repin`)."""
    pattern = re.compile(r'(?m)^(horizon_revision\s*=\s*)"[^"]*"')
    if pattern.search(text):
        return pattern.sub(lambda m: f'{m.group(1)}"{commit}"', text)
    return re.sub(r"(?m)^(\[compatibility\][ \t]*\r?\n)", rf'\g<1>horizon_revision = "{commit}"\n', text, count=1)


def stage(bundles, commit, out):
    """Writes each mod to out/<id>/ as horizon-mixins wants it. Returns what was repinned."""
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    repinned = []
    for bundle in bundles:
        dest = out / bundle.id
        for rel, data in bundle.files.items():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        path = dest / MIXIN
        text = path.read_text(encoding="utf-8")
        new = _drop_table(text, "dependencies")
        if bundle.horizon and bundle.horizon != commit:
            new = _repin(new, commit)
            repinned.append(bundle.id)
        if new != text:
            check = tomllib.loads(new)
            if "dependencies" in check or (check.get("mod") or {}).get("id") != bundle.id or (
                    bundle.horizon and (check.get("compatibility") or {}).get("horizon_revision") != commit):
                raise ModError(f"{bundle.source}: could not rewrite {MIXIN} safely")
            path.write_text(new, encoding="utf-8", newline="\n")
    return repinned


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["list", "stage"])
    parser.add_argument("--mods", type=Path, default=Path("mods"))
    parser.add_argument("--select", default="all")
    parser.add_argument("--commit")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    try:
        bundles = discover(args.mods, args.select)
        if args.command == "list":
            print(json.dumps([b.summary() for b in bundles], indent=2))
            return
        if not (args.commit and is_revision(args.commit) and args.out):
            parser.error("stage needs --commit (40 hex) and --out")
        repinned = stage(bundles, args.commit, args.out)
        for bundle in bundles:
            print(f"staged {bundle.id} {bundle.version} ({bundle.source})" + (" [repinned]" if bundle.id in repinned else ""))
    except (ModError, ValueError, OSError) as error:
        print(f"::error::{error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
