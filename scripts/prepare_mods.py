#!/usr/bin/env python3
"""Validate .hnmod archives and stage compatible bundles for horizon-mixins.

  extract UPLOADS OUT     check every .hnmod in UPLOADS and unpack its bundle into OUT/bundles/<id>
  stage ...               choose the Horizon commit the mods build on, install horizon-mixins, apply the mods

Checks follow Horizon's own `Bundle::read` and the launcher (docs/native-mods.md): no links or `..`, only
Rust, TOML and `data/` files under `mixins/<id>/`, at most 512 entries, and the manifest and the mixin file
must agree. Mods the launcher builds with extra machinery this builder doesn't have (an `[api]` crate,
`trait_method` injections, `[[post_build]]` steps) are refused by name instead of building something wrong.
"""
import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
import zipfile

MAX_MODS = 10
MAX_FILE = 40 * 1024 * 1024
MAX_TOTAL = 200 * 1024 * 1024
MAX_ENTRIES = 512
UPSTREAM = os.environ.get("UPSTREAM", "horizon-rust/horizon")
MIXINS_REPO = "https://github.com/horizon-rust/horizon-mixins"
MIXINS_SLUG = "horizon-rust/horizon-mixins"


def fail(message):
    raise SystemExit("::error::" + message)


def date_key(revision, repository=UPSTREAM):
    """When `repository`'s commit `revision` was made (ISO 8601, so it sorts)."""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/commits/{revision}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "horizon-client-builder"},
    )
    if os.environ.get("GH_TOKEN"):
        req.add_header("Authorization", f"Bearer {os.environ['GH_TOKEN']}")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)["commit"]["committer"]["date"]
    except urllib.error.HTTPError as error:
        fail(f"Could not resolve {repository} commit {revision}: GitHub HTTP {error.code}")


def allowed(rest):
    """Whether a bundle may carry the file `rest` (its path under mixins/<id>/): Bundle::read's `allowed`."""
    parts = rest.split("/")
    in_data = len(parts) >= 2 and (parts[0] == "data" or (len(parts) >= 3 and parts[1] == "data"))
    name = parts[-1]
    text = len(parts) <= 2 and (
        any(name.startswith(p) for p in ("README", "LICENSE", "LICENCE", "COPYING", "NOTICE")) or name == ".gitignore"
    )
    return rest.endswith(".rs") or rest.endswith(".toml") or in_data or text


def unsupported(manifest):
    """Why a lone horizon-client.exe can't be built with this mod here, or None."""
    if "api" in manifest:
        return "has an [api] trait crate; the launcher builds those with an extra generated game package this builder doesn't have"
    if any(isinstance(i, dict) and "trait_method" in i for i in manifest.get("injection", [])):
        return "uses trait_method injections, which need an [api] crate this builder doesn't have"
    if "post_build" in manifest:
        return "has [[post_build]] steps, which the launcher runs inside an installed Horizon version folder; a lone exe has none"
    return None


def safe_extract(archive, destination):
    total = entries = 0
    with zipfile.ZipFile(archive) as zf:
        for item in zf.infolist():
            name = pathlib.PurePosixPath(item.filename)
            if (name.is_absolute() or ".." in name.parts or not name.parts
                    or "\\" in item.filename or ":" in item.filename):
                fail(f"{archive.name}: unsafe path {item.filename!r}")
            mode = item.external_attr >> 16
            if mode and (mode & 0o170000) == 0o120000:
                fail(f"{archive.name}: symlinks are not allowed")
            if item.is_dir():
                continue
            if item.filename == "mod.wasm":
                fail(f"{archive.name}: WebAssembly mods don't run any more; port the code to a native mod")
            entries += 1
            total += item.file_size
            if entries > MAX_ENTRIES:
                fail(f"{archive.name}: native bundle has too many entries")
            if item.file_size > MAX_FILE or total > MAX_TOTAL:
                fail(f"{archive.name}: expanded mod files exceed size limits")
            target = destination.joinpath(*name.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(item) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def hooks_version(archive, raw):
    """`hooks` is optional (absent: the bundle pins the exact Horizon commit). Numeric strings are accepted:
    some third-party packagers write this JSON field as text."""
    if raw is None:
        return 0
    if isinstance(raw, bool):
        fail(f"{archive.name}: invalid hooks API version")
    if isinstance(raw, int):
        hooks = raw
    elif isinstance(raw, str) and raw.isdecimal():
        hooks = int(raw)
    else:
        fail(f"{archive.name}: invalid hooks API version")
    if hooks < 0:
        fail(f"{archive.name}: invalid hooks API version")
    return hooks


def extract(args):
    files = sorted(pathlib.Path(args.uploads).glob("*.hnmod"))
    if not files:
        fail("No .hnmod files were uploaded.")
    if len(files) > MAX_MODS:
        fail(f"At most {MAX_MODS} .hnmod files are allowed.")
    if any(p.stat().st_size > MAX_FILE for p in files):
        fail("A .hnmod file exceeds the 40 MiB limit.")
    out = pathlib.Path(args.out)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    records, total = [], 0
    for archive in files:
        temp = out / (archive.stem + ".unpacked")
        temp.mkdir()
        try:
            safe_extract(archive, temp)
        except zipfile.BadZipFile:
            fail(f"{archive.name}: not a zip file")
        meta_path = temp / "horizon-native.json"
        if not meta_path.is_file():
            fail(f"{archive.name}: missing horizon-native.json")
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except ValueError as error:
            fail(f"{archive.name}: horizon-native.json is not valid JSON: {error}")
        # Horizon's valid_id: 1 to 64 of a-z, 0-9 and _ (no hyphen).
        if meta.get("schema") != 1 or not re.fullmatch(r"[a-z0-9_]{1,64}", str(meta.get("id", ""))):
            fail(f"{archive.name}: unsupported or invalid native-mod manifest")
        mod_id = meta["id"]
        bundle = temp / "mixins" / mod_id
        manifest_path = bundle / "horizon-mixin.toml"
        if not manifest_path.is_file():
            fail(f"{archive.name}: missing mixins/{mod_id}/horizon-mixin.toml")
        try:
            manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as error:
            fail(f"{archive.name}: horizon-mixin.toml is not valid TOML: {error}")
        if manifest.get("mod", {}).get("id") != mod_id:
            fail(f"{archive.name}: manifest id does not match {mod_id}")
        if not re.fullmatch(r"[0-9a-f]{40}", meta.get("horizon", "")):
            fail(f"{archive.name}: invalid Horizon revision pin")
        if not re.fullmatch(r"[0-9a-f]{40}", meta.get("mixins", "")):
            fail(f"{archive.name}: invalid horizon-mixins revision pin")
        hooks = hooks_version(archive, meta.get("hooks"))
        if mod_id in [r["id"] for r in records]:
            fail(f"Duplicate mod ID: {mod_id}")
        # The archive's compatibility metadata is authoritative; reject internally inconsistent bundles.
        if manifest.get("compatibility", {}).get("horizon_revision") != meta["horizon"]:
            fail(f"{archive.name}: horizon-mixin.toml and horizon-native.json pins disagree")
        for path in sorted(p for p in bundle.rglob("*") if p.is_file()):
            rest = path.relative_to(bundle).as_posix()
            if not allowed(rest):
                fail(f"{archive.name}: unexpected native source: mixins/{mod_id}/{rest} (only Rust, TOML and data files)")
        why = unsupported(manifest)
        if why:
            fail(f"{archive.name} ({mod_id}) {why}.")
        target = out / "bundles" / mod_id
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(bundle, target)
        records.append({"id": mod_id, "version": meta.get("version", "unknown"),
                        "horizon": meta["horizon"], "mixins": meta["mixins"], "hooks": hooks,
                        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()})
        total += archive.stat().st_size
        shutil.rmtree(temp)
    if total > MAX_TOTAL:
        fail("Total uploaded .hnmod size exceeds 200 MiB.")
    (out / "mods.json").write_text(json.dumps(records, indent=2) + "\n")
    print(json.dumps({"mods": [{k: v for k, v in r.items() if k != "sha256"} for r in records],
                      "total_bytes": total}, indent=2))


def digest(records):
    """Eight hex digits naming this exact set of mods (their files, not just their ids)."""
    text = "|".join(sorted(f"{r['id']}@{r['sha256']}" for r in records))
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def mixins_revision(horizon_tree):
    source = pathlib.Path(horizon_tree) / "launcher/src/native_mods.rs"
    match = re.search(r'pub const MIXINS_REV: &str = "([0-9a-f]{40})"', source.read_text(encoding="utf-8"))
    if not match:
        fail("Could not read Horizon's own pinned horizon-mixins revision.")
    return match.group(1)


def build_cli(revision, root):
    """horizon-mixins at `revision`, installed once into <root>/<revision[:12]>/ the way the launcher does it
    (`cargo install --locked --git --rev --root`). That never reads the checkout's rust-toolchain.toml, which
    would download a second Rust, and a tool that is already there (restored from the cache) is used as it is.
    -> (binary, built now)"""
    dest = pathlib.Path(root).resolve() / revision[:12]
    binary = dest / "bin" / ("horizon-mixins.exe" if os.name == "nt" else "horizon-mixins")
    if binary.is_file():
        print(f"horizon-mixins {revision[:7]}: from the cache")
        return binary, False
    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    for var in ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "CARGO_INCREMENTAL"):  # the tool isn't the game
        env.pop(var, None)
    print(f"horizon-mixins {revision[:7]}: building", flush=True)
    subprocess.run(["cargo", "install", "--locked", "--git", MIXINS_REPO, "--rev", revision,
                    "--root", str(dest), "horizon-mixins-cli"], env=env, check=True)
    if not binary.is_file():
        fail(f"cargo install did not produce {binary}")
    return binary, True


def drop_table(text, name):
    """`text` without the launcher-only `[name]` table (the launcher strips [api] and [dependencies])."""
    out, skipping = [], False
    for line in text.splitlines(keepends=True):
        header = re.match(r'\s*\[\[?\s*([A-Za-z0-9_."-]+)\s*\]\]?', line)
        if header:
            key = header.group(1)
            skipping = key == name or key.startswith(name + ".")
        if not skipping:
            out.append(line)
    return "".join(out)


def set_output(**values):
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            for key, value in values.items():
                out.write(f"{key}={value}\n")



def patch_horizon_compat(tree):
    """Restore the public frame helper expected by older voice-chat bundles."""
    frame = pathlib.Path(tree) / "crates" / "horizon-protocol" / "src" / "frame.rs"
    if not frame.is_file():
        return
    source = frame.read_text(encoding="utf-8")
    if re.search(r"(?m)^pub\s+fn\s+decompress\s*\(", source):
        return
    tests = re.search(r"(?m)^#\[cfg\(test\)\]\s*\nmod tests\b", source)
    if not tests:
        return
    shim = """/// Compatibility entry point used by older native voice-chat bundles.
pub fn decompress(frame: Vec<u8>, threshold: Option<usize>) -> Result<Vec<u8>> {
    Ok(match body(&frame, threshold, &mut Inflater::default())? {
        Body::Plain(at) => frame[at..].to_vec(),
        Body::Inflated(body) => body,
    })
}

"""
    frame.write_text(source[:tests.start()] + shim + source[tests.start():], encoding="utf-8", newline="\n")
    print("Added horizon_protocol::frame::decompress compatibility shim")


def normalize_target_lifetimes(manifest_text):
    """Remove elided lifetimes from inherent-method target IDs."""
    pattern = r"""(?m)^([ \t]*target[ \t]*=[ \t]*"[^"\n]*)<'_>([^"\n]*")"""
    return re.sub(pattern, lambda match: match.group(1) + match.group(2), manifest_text)


def stage(args):
    mods_dir = pathlib.Path(args.mods)
    records = json.loads((mods_dir / "mods.json").read_text())
    requested = args.requested
    out = pathlib.Path(args.out)
    tree = pathlib.Path(args.horizon_root)
    hook_mods = [m for m in records if m["hooks"] > 0]
    pinned_mods = [m for m in records if m["hooks"] == 0]
    candidates = sorted({m["horizon"] for m in records}, key=date_key, reverse=True)
    # Hook-only mods can target the selected build when its append-only hook API is new enough.
    if requested and hook_mods and not pinned_mods:
        hooks_file = tree / "crates/horizon-client/src/game/native_hooks.rs"
        match = re.search(r"pub\(crate\) const NATIVE_HOOKS_API: u32 = (\d+);", hooks_file.read_text(encoding="utf-8"))
        available = int(match.group(1)) if match else -1
        if available >= max(m["hooks"] for m in hook_mods):
            candidates = [requested]
    if not candidates:
        candidates = [requested]
    errors, built_any = [], False
    for revision in candidates:
        # The requested commit is already unpacked in `tree`: don't fetch it again.
        marker = tree / ".horizon-revision"
        if not (marker.is_file() and marker.read_text().strip() == revision):
            subprocess.run([sys.executable, args.fetch_source, revision, str(tree)], check=True)
        patch_horizon_compat(tree)
        mixins_candidates = {mixins_revision(tree), *(m["mixins"] for m in records)}
        mixins_rev = max(mixins_candidates, key=lambda rev: date_key(rev, MIXINS_SLUG))
        cli, built = build_cli(mixins_rev, args.mixins_cache)
        built_any = built_any or built
        candidate_mods = out / "mods" / revision
        shutil.rmtree(candidate_mods, ignore_errors=True)
        candidate_mods.mkdir(parents=True)
        for record in records:
            dst = candidate_mods / record["id"]
            shutil.copytree(mods_dir / "bundles" / record["id"], dst)
            manifest = dst / "horizon-mixin.toml"
            text = drop_table(manifest.read_text(encoding="utf-8"), "dependencies")
            text = normalize_target_lifetimes(text)
            # Each mod is moved to the commit that is built (the launcher's `repin`).
            text = re.sub(r'(horizon_revision\s*=\s*")[0-9a-f]{40}(")', rf"\g<1>{revision}\2", text)
            manifest.write_text(text, encoding="utf-8", newline="\n")
        modded = out / "modded"
        shutil.rmtree(modded, ignore_errors=True)
        # `apply` plans from pristine source itself and stages atomically, so a separate `plan` run only
        # indexes the whole workspace twice.
        applied = subprocess.run([str(cli), "apply", "--workspace", str(tree), "--mods", str(candidate_mods),
                                  "--out", str(modded)], text=True, capture_output=True)
        if applied.returncode:
            errors.append(f"{revision[:7]}: {(applied.stderr or applied.stdout)[-3000:]}")
            continue
        chosen = {"horizon": revision, "requested": requested, "mixins": mixins_rev, "mods": [
            {k: v for k, v in r.items() if k != "sha256"} for r in records]}
        (out / "chosen.json").write_text(json.dumps(chosen, indent=2) + "\n")
        print(f"Selected Horizon {revision}; horizon-mixins {mixins_rev}")
        set_output(horizon=revision, mixins=mixins_rev, built_mixins=str(built_any).lower(), digest=digest(records),
                   mods=json.dumps(chosen["mods"], separators=(",", ":")))
        return
    set_output(built_mixins=str(built_any).lower())
    fail("No single Horizon revision accepts every selected mod.\n" + "\n".join(errors))


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("extract")
    p.add_argument("uploads"); p.add_argument("out")
    p.set_defaults(func=extract)
    p = sub.add_parser("stage")
    p.add_argument("--mods", required=True); p.add_argument("--requested", required=True)
    p.add_argument("--horizon-root", required=True); p.add_argument("--fetch-source", required=True)
    p.add_argument("--mixins-cache", required=True); p.add_argument("--out", required=True)
    p.set_defaults(func=stage)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
