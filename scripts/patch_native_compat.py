#!/usr/bin/env python3
"""Toolchain compatibility shims for the staged native mod crates.

Mods often vendor C code that assumes a GCC/Clang toolchain. The clearest
known case is opus built with -DVAR_ARRAYS: that mode implements ALLOC()
with C99 variable-length arrays, which MSVC's cl.exe cannot compile - the
builder's first Windows mod build failed with exactly that (C2057 /
C2466 / C2133 on every ALLOC declaration in opus_decoder.c). Opus's own
Windows builds and every Rust opus binding use USE_ALLOCA instead: the
same stack-allocation semantics, no VLAs, supported by every toolchain
Horizon builds on (MSVC, GCC, Clang).

This script rewrites those build-script defines inside the staged mod
crates (game-stage/native-crates/**/build.rs) when the build target is
Windows MSVC, then reports every change as JSON so the build log shows
exactly what was shimmed. The patch is source-level and idempotent: a
second run on the same stage changes nothing.

Extending: add entries to SHIMS (old definition -> portable replacement).
Keep the list to proven, semantically equivalent toolchain workarounds -
never behavior changes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Known MSVC-incompatible build-script definitions and their portable
# replacements, applied only to build.rs files of staged native mod crates.
SHIMS: dict[str, str] = {
    # C99 variable-length arrays are not implemented by cl.exe.
    "VAR_ARRAYS": "USE_ALLOCA",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, default=Path("game-stage"),
                        help="staged workspace root containing native-crates/ "
                             "(default: game-stage)")
    parser.add_argument("--target", required=True,
                        help="Rust target triple, e.g. x86_64-pc-windows-msvc")
    args = parser.parse_args()

    def emit(result: dict) -> None:
        print(json.dumps(result, indent=2))
        # Mirror the report into the run summary and expose it to later
        # steps (build-info.txt) when running inside Actions.
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        github_env = os.environ.get("GITHUB_ENV")
        if summary:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write("### Native-crate toolchain shims\n\n")
                if result.get("patched_files"):
                    handle.write("```json\n")
                    handle.write(json.dumps(result, indent=2))
                    handle.write("\n```\n")
                else:
                    handle.write("None needed "
                                 f"({result.get('note', 'nothing to shim')}).\n")
        if github_env:
            with open(github_env, "a", encoding="utf-8") as handle:
                report = json.dumps(result)
                if "\n" in report:  # keep the heredoc safe for any input
                    handle.write(f"COMPAT_PATCH_REPORT<<COMPAT_EOF\n{report}\nCOMPAT_EOF\n")
                else:
                    handle.write(f"COMPAT_PATCH_REPORT={report}\n")

    if not args.target.endswith("windows-msvc"):
        emit({"target": args.target, "patched_files": [],
              "note": "shims only apply to windows-msvc targets"})
        return 0

    crates = args.stage / "native-crates"
    if not crates.is_dir():
        emit({"target": args.target, "patched_files": [],
              "note": "no staged native crates"})
        return 0

    patched = []
    for build_rs in sorted(crates.rglob("build.rs")):
        try:
            text = build_rs.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"Could not read {build_rs}: {exc}", file=sys.stderr)
            return 2
        changes: dict[str, dict] = {}
        for old, new in SHIMS.items():
            count = text.count(old)
            if count:
                text = text.replace(old, new)
                changes[old] = {"replacement": new, "occurrences": count}
        if changes:
            try:
                build_rs.write_text(text, encoding="utf-8", newline="\n")
            except OSError as exc:
                print(f"Could not write {build_rs}: {exc}", file=sys.stderr)
                return 2
            patched.append({"file": build_rs.as_posix(), "changes": changes})

    emit({"target": args.target, "patched_files": patched,
          "note": "patched" if patched else "no shim patterns found"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
