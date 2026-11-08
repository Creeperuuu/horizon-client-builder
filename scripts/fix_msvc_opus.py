#!/usr/bin/env python3
"""Use Opus's alloca path when compiling its C sources with MSVC.

MSVC does not implement C99 variable-length arrays. Opus builds that define VAR_ARRAYS make declarations
such as `type buffer[size]` fail with C2057/C2466. Opus already provides USE_ALLOCA for this exact case,
using _alloca on Windows.

A mod can vendor Opus anywhere in its crates, so every `stack_alloc.h` under <horizon>/native-crates that
mentions VAR_ARRAYS is fixed, not one mod's path. With no native mod crates, or no such header, there is
nothing to do and it succeeds quietly: a clean client must not fail here.

usage: fix_msvc_opus.py [HORIZON_DIR]    (default: horizon)
"""
import sys
from pathlib import Path

MARKER = "#if defined(_MSC_VER) && defined(VAR_ARRAYS)\n#undef VAR_ARRAYS\n#define USE_ALLOCA\n#endif\n"
GUARD = "#define STACK_ALLOC_H\n"


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "horizon") / "native-crates"
    if not root.is_dir():
        print("No native mod crates: nothing to configure for MSVC.")
        return 0
    fixed = seen = 0
    for header in sorted(root.rglob("stack_alloc.h")):
        text = header.read_text(encoding="utf-8", errors="replace")
        if "VAR_ARRAYS" not in text:
            continue
        if MARKER in text:
            print(f"{header}: already configured.")
            seen += 1
            continue
        if GUARD not in text:
            print(f"::error::Unrecognized Opus stack_alloc.h layout: {header}")
            return 1
        header.write_text(text.replace(GUARD, GUARD + "\n" + MARKER, 1), encoding="utf-8", newline="\n")
        print(f"{header}: configured to use _alloca with MSVC.")
        fixed += 1
    if not fixed and not seen:
        print("No bundled Opus needing the MSVC allocation fix.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
