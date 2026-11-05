#!/usr/bin/env python3
"""Select one Horizon revision for a mixed-pin native-mod build."""
import argparse
import json
import os
import re
import subprocess
import sys


def run_git(workspace, *args):
    return subprocess.run(
        ["git", "-C", workspace, *args],
        capture_output=True,
        text=True,
        check=False,
    )


def pins_from_env():
    raw = os.environ.get("PINS_JSON", "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid PINS_JSON: {exc}")
    pins = data.get("pins", []) if isinstance(data, dict) else []
    if not isinstance(pins, list):
        raise SystemExit("PINS_JSON.pins must be an array")
    clean = []
    for pin in pins:
        if not isinstance(pin, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", pin):
            raise SystemExit(f"Invalid Horizon revision pin: {pin!r}")
        clean.append(pin.lower())
    return sorted(set(clean))


def check_opt_in(pins):
    if len(pins) > 1 and os.environ.get("ALLOW_MIXED_REVISIONS", "false").lower() != "true":
        print("Selected mods pin different Horizon commits:", file=sys.stderr)
        for pin in pins:
            print(f"  {pin}", file=sys.stderr)
        print("Select 'Build anyway with mixed-revision mods' to try the newest pin.", file=sys.stderr)
        return False
    if len(pins) > 1:
        print("Mixed Horizon pins accepted; will try the newest pinned commit.")
    return True


def append_github_file(variable, line):
    path = os.environ.get(variable)
    if path:
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default="horizon")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    pins = pins_from_env()
    if not check_opt_in(pins):
        return 2
    if args.check_only:
        print("Revision-pin approval check passed.")
        return 0

    if pins:
        dated = []
        for pin in pins:
            result = run_git(args.workspace, "rev-list", "-n", "1", "--timestamp", pin)
            fields = result.stdout.strip().split()
            if result.returncode != 0 or len(fields) < 2 or not fields[0].isdigit():
                raise SystemExit(
                    f"Could not resolve pinned Horizon commit {pin} in the checkout; "
                    "fetch that revision or correct the mod pin."
                )
            dated.append((int(fields[0]), pin))
        selected = max(dated)[1]
        result = run_git(args.workspace, "checkout", "--force", "--detach", selected)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise SystemExit(f"Could not check out selected Horizon commit {selected}: {detail}")
        reason = "newest mod pin" if len(pins) > 1 else "the mods' pinned commit"
    else:
        result = run_git(args.workspace, "rev-parse", "HEAD")
        if result.returncode != 0:
            raise SystemExit("Could not resolve the requested Horizon revision.")
        selected = result.stdout.strip()
        reason = "requested Horizon ref (no mod pins)"

    result = run_git(args.workspace, "rev-parse", "HEAD")
    if result.returncode != 0:
        raise SystemExit("Could not resolve the selected Horizon commit after checkout.")
    selected = result.stdout.strip()
    print(f"Selected Horizon revision {selected} ({reason}).")
    append_github_file("GITHUB_OUTPUT", f"revision={selected}")
    append_github_file("GITHUB_ENV", f"FINAL_HORIZON_COMMIT={selected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
