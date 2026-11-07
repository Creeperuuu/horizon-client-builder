#!/usr/bin/env python3
"""Validate .hnmod archives and stage compatible bundles for horizon-mixins."""
import argparse
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
UPSTREAM = os.environ.get("UPSTREAM", "horizon-rust/horizon")
MIXINS_REPO = "https://github.com/horizon-rust/horizon-mixins"


def fail(message):
    raise SystemExit("::error::" + message)


def commit_date(revision):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{UPSTREAM}/commits/{revision}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "horizon-client-builder"},
    )
    token = os.environ.get("GH_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    import datetime
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.read().decode()
    except urllib.error.HTTPError as error:
        fail(f"Could not resolve commit date for {revision}: GitHub HTTP {error.code}")


def date_key(revision, repository=UPSTREAM):
    url = f"https://api.github.com/repos/{repository}/commits/{revision}"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "horizon-client-builder"})
    token = os.environ.get("GH_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)["commit"]["committer"]["date"]
    except urllib.error.HTTPError as error:
        fail(f"Could not resolve {repository} commit {revision}: GitHub HTTP {error.code}")

def safe_extract(archive, destination):
    total = 0
    with zipfile.ZipFile(archive) as zf:
        for item in zf.infolist():
            name = pathlib.PurePosixPath(item.filename)
            if name.is_absolute() or ".." in name.parts or not name.parts:
                fail(f"{archive.name}: unsafe path {item.filename!r}")
            mode = item.external_attr >> 16
            if mode and (mode & 0o170000) == 0o120000:
                fail(f"{archive.name}: symlinks are not allowed")
            if item.is_dir():
                continue
            total += item.file_size
            if item.file_size > MAX_FILE or total > MAX_TOTAL:
                fail(f"{archive.name}: expanded mod files exceed size limits")
            target = destination.joinpath(*name.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(item) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)


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
        safe_extract(archive, temp)
        meta_path = temp / "horizon-native.json"
        if not meta_path.is_file():
            fail(f"{archive.name}: missing horizon-native.json")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("schema") != 1 or not re.fullmatch(r"[a-z0-9_-]{1,64}", meta.get("id", "")):
            fail(f"{archive.name}: unsupported or invalid native-mod manifest")
        mod_id = meta["id"]
        bundle = temp / "mixins" / mod_id
        manifest_path = bundle / "horizon-mixin.toml"
        if not manifest_path.is_file():
            fail(f"{archive.name}: missing mixins/{mod_id}/horizon-mixin.toml")
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("mod", {}).get("id") != mod_id:
            fail(f"{archive.name}: manifest id does not match {mod_id}")
        if not re.fullmatch(r"[0-9a-f]{40}", meta.get("horizon", "")):
            fail(f"{archive.name}: invalid Horizon revision pin")
        if not re.fullmatch(r"[0-9a-f]{40}", meta.get("mixins", "")):
            fail(f"{archive.name}: invalid horizon-mixins revision pin")
        raw_hooks = meta.get("hooks", 0)
        # "hooks" is optional: its absence means this bundle pins the exact
        # Horizon commit above. Accept numeric strings too, since some
        # third-party packagers serialize this JSON field as text.
        if raw_hooks is None:
            hooks = 0
        elif isinstance(raw_hooks, bool):
            fail(f"{archive.name}: invalid hooks API version")
        elif isinstance(raw_hooks, int):
            hooks = raw_hooks
        elif isinstance(raw_hooks, str) and raw_hooks.isdecimal():
            hooks = int(raw_hooks)
        else:
            fail(f"{archive.name}: invalid hooks API version")
        if hooks < 0:
            fail(f"{archive.name}: invalid hooks API version")
        if mod_id in [r["id"] for r in records]:
            fail(f"Duplicate mod ID: {mod_id}")
        # The archive's compatibility metadata is authoritative; reject internally inconsistent bundles.
        pin = manifest.get("compatibility", {}).get("horizon_revision")
        if pin != meta["horizon"]:
            fail(f"{archive.name}: horizon-mixin.toml and horizon-native.json pins disagree")
        target = out / "bundles" / mod_id
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(bundle, target)
        records.append({"id": mod_id, "version": meta.get("version", "unknown"),
                        "horizon": meta["horizon"], "mixins": meta["mixins"], "hooks": hooks})
        total += archive.stat().st_size
        shutil.rmtree(temp)
    if total > MAX_TOTAL:
        fail("Total uploaded .hnmod size exceeds 200 MiB.")
    (out / "mods.json").write_text(json.dumps(records, indent=2) + "\n")
    print(json.dumps({"mods": records, "total_bytes": total}, indent=2))


def mixins_revision(horizon_tree):
    source = pathlib.Path(horizon_tree) / "launcher/src/native_mods.rs"
    text = source.read_text(encoding="utf-8")
    match = re.search(r'pub const MIXINS_REV: &str = "([0-9a-f]{40})"', text)
    if not match:
        fail("Could not read Horizon's own pinned horizon-mixins revision.")
    return match.group(1)


def build_cli(revision, root):
    # Keep the Cargo target directory stable across workflow runs so rust-cache can persist it.
    dest = pathlib.Path(root) / revision
    target_dir = pathlib.Path(root).parent / "mixins-cache-target"
    binary = target_dir / "release" / ("horizon-mixins.exe" if os.name == "nt" else "horizon-mixins")
    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        ["git", "clone", "--filter=blob:none", MIXINS_REPO, str(dest)],
        check=True
    )
    subprocess.run(["git", "-C", str(dest), "checkout", "--detach", revision], check=True)
    env = os.environ.copy()
    env["CARGO_TARGET_DIR"] = str(target_dir.resolve())
    subprocess.run(["cargo", "build", "--locked", "--release", "-p", "horizon-mixins-cli"], cwd=dest, env=env, check=True)
    return binary
def stage(args):
    mods_dir = pathlib.Path(args.mods)
    records = json.loads((mods_dir / "mods.json").read_text())
    requested = args.requested
    hook_mods = [m for m in records if m["hooks"] > 0]
    pinned_mods = [m for m in records if m["hooks"] == 0]
    candidates = sorted({m["horizon"] for m in records}, key=date_key, reverse=True)
    # Hook-only mods can target the selected build when its append-only hook API is new enough.
    if requested and hook_mods and not pinned_mods:
        hooks_file = pathlib.Path(args.horizon_root) / "crates/horizon-client/src/game/native_hooks.rs"
        match = re.search(r"pub\(crate\) const NATIVE_HOOKS_API: u32 = (\d+);", hooks_file.read_text(encoding="utf-8"))
        available = int(match.group(1)) if match else -1
        if available >= max(m["hooks"] for m in hook_mods):
            candidates = [requested]
    if not candidates:
        candidates = [requested]
    errors = []
    for revision in candidates:
        tree = pathlib.Path(args.horizon_root)
        subprocess.run([sys.executable, args.fetch_source, revision, str(tree)], check=True)
        mixins_candidates = {mixins_revision(tree), *(m["mixins"] for m in records)}
        mixins_rev = max(mixins_candidates, key=lambda rev: date_key(rev, "horizon-rust/horizon-mixins"))
        cli = build_cli(mixins_rev, args.mixins_cache)
        candidate_mods = pathlib.Path(args.out) / revision
        shutil.rmtree(candidate_mods, ignore_errors=True)
        candidate_mods.mkdir(parents=True)
        for record in records:
            src = mods_dir / "bundles" / record["id"]
            dst = candidate_mods / record["id"]
            shutil.copytree(src, dst)
            manifest = dst / "horizon-mixin.toml"
            text = manifest.read_text(encoding="utf-8")
            text = re.sub(r'(horizon_revision\s*=\s*")[0-9a-f]{40}(")', rf'\g<1>{revision}\2', text)
            manifest.write_text(text, encoding="utf-8")
        base = ["--workspace", str(tree), "--mods", str(candidate_mods)]
        plan = subprocess.run([str(cli), "plan", *base, "--out", str(pathlib.Path(args.out) / "plan.json")],
                              text=True, capture_output=True)
        if plan.returncode:
            errors.append(f"{revision[:7]}: {plan.stderr[-3000:]}")
            continue
        applied = subprocess.run([str(cli), "apply", *base, "--out", str(pathlib.Path(args.out) / "modded")],
                                 text=True, capture_output=True)
        if applied.returncode:
            errors.append(f"{revision[:7]} apply: {applied.stderr[-3000:]}")
            continue
        (pathlib.Path(args.out) / "chosen.json").write_text(json.dumps(
            {"horizon": revision, "mixins": mixins_rev, "mods": records}, indent=2) + "\n")
        print(f"Selected Horizon {revision}; horizon-mixins {mixins_rev}")
        return
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
