# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Excido is a fuzzing harness generator that parses C/C++ source code using Clang AST, identifies target functions, resolves type dependencies, and generates AFL++ fuzzing harnesses.

## Build Commands

### C++ (CMake + Ninja + vcpkg)

```bash
# Configure (requires system LLVM/Clang dev libraries)
cmake -B build -G Ninja

# Build
cmake --build build

# Binaries: build/excido, build/func_scanner
```

### Python (uv)

Always use `uv run python` to run Python scripts (never `python3` directly):

```bash
uv run python -m excido.fuzz_builder -p <source_path> -f <function_name> [-v]
```

Python version is managed by asdf (see `.tool-versions`).

## Architecture

The project has two parallel implementations of the same core logic:

- **Python path** (`src/excido/fuzz_builder.py`): The original implementation using Clang Python bindings. Orchestrates AST traversal, function discovery, type resolution, and harness generation.
- **C++ path** (`src/excido/fuzz_builder.cpp`): Active port using libclang and Clang Tooling C++ APIs directly. Work in progress.

Supporting C++ files:
- `searcher.cpp` — SSE2-optimized code search
- `cc_adjuster.cpp` — Custom compiler argument adjuster for cross-compilation

Supporting Python modules:
- `ast_types.py` — Maps between Clang AST CursorKind enums and string representations
- `ast_logging.py` — Log capture context manager
- `ast_exceptions.py` — Custom exceptions

## Working Practices

- Always dump command output to a file (e.g. `/tmp/fuzz_builder_output.txt`) so you never have to run it more than once

## Key Details

- C++23 standard, compiled with clang/clang++
- Clang-format style is Mozilla-based (see `.clang-format`), 80-column limit, 2-space indent
- LLVM/Clang libraries linked: libclang, clangAST, clangASTMatchers, clangBasic, clangFrontend, clangIndex, clangSerialization, clangTooling
- Fuzz harness template lives at `utils/share/fuzz.c.template`
- Compilation database cache at `utils/cache/compile_commands.json`
- No tests exist yet
