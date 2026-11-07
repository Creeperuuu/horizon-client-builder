#!/usr/bin/env python3
"""Use Opus's alloca path when compiling its C sources with MSVC.

MSVC does not implement C99 variable-length arrays. The bundled Opus build
currently defines VAR_ARRAYS unconditionally, which makes declarations such
as type buffer[size] fail with C2057/C2466. Opus already provides USE_ALLOCA
for this exact case, using _alloca on Windows.
"""
from pathlib import Path

header = Path("horizon/native-crates/voicechat/engine/data/opus/celt/stack_alloc.h")
if not header.is_file():
    raise SystemExit(f"Expected bundled Opus header not found: {header}")

text = header.read_text(encoding="utf-8")
marker = "#if defined(_MSC_VER) && defined(VAR_ARRAYS)\n#undef VAR_ARRAYS\n#define USE_ALLOCA\n#endif\n"
if marker in text:
    print("Opus MSVC allocation compatibility is already applied.")
    raise SystemExit(0)

guard = "#define STACK_ALLOC_H\n"
if guard not in text:
    raise SystemExit(f"Unrecognized Opus stack_alloc.h layout: {header}")

text = text.replace(guard, guard + "\n" + marker, 1)
header.write_text(text, encoding="utf-8", newline="\n")
print("Configured bundled Opus to use _alloca with MSVC.")
