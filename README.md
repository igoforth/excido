# excido

Fuzzing harness generator for C/C++ libraries. Given a function name and a `compile_commands.json`, excido walks the Clang AST, resolves every type dependency transitively across translation units, and emits a single self-contained `.c` file with an AFL++ harness skeleton.

## Requirements

- Python 3.14+ (managed by [asdf](https://asdf-vm.com/))
- [uv](https://docs.astral.sh/uv/) for Python dependency management
- LLVM/Clang 18+ (for libclang Python bindings and clang-format)
- CMake + Ninja (for building func_scanner)
- [Coccinelle](https://coccinelle.gitlabpages.inria.fr/website/) (optional, for caller discovery)

## Building

### func_scanner (C++ function indexer)

Requires LLVM/Clang development libraries installed on your system.

```bash
cmake -B build -G Ninja
cmake --build build --target func_scanner
```

The binary is at `build/func_scanner`. It indexes all function definitions in a compile database using Clang's preprocessor (no AST, fast).

### Python dependencies

```bash
uv sync
```

## Usage

```bash
uv run python -m excido.fuzz_builder \
    -f <function_name> \
    -c <path/to/compile_commands.json> \
    -s <path/to/stubs.h> \
    [-v] [--rw-analysis] [--log-file <path>]
```

This writes `fuzz.c` in the current directory.

**Arguments:**

| Flag | Description |
|------|-------------|
| `-f` | Target function name |
| `-c` | Path to `compile_commands.json` |
| `-s` | Path to stubs file (see below) |
| `-v` | Verbose logging to stderr |
| `--rw-analysis` | Analyze pointer parameter read/write counts (slow) |
| `--log-file` | Structured JSONL log output (default: `excido.log.jsonl`) |

## Example: mbedTLS

Clone and configure mbedTLS to generate a compile database:

```bash
git clone --depth 1 https://github.com/Mbed-TLS/mbedtls.git
cd mbedtls
git submodule update --init --recursive
cmake -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
cd ..
```

Create a stubs file (`stubs_mbedtls.h`):

```c
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* Blacklist standard types to prevent cross-platform typedef conflicts */
typedef size_t size_t;
typedef uint8_t uint8_t;
typedef uint16_t uint16_t;
typedef uint32_t uint32_t;
typedef uint64_t uint64_t;
typedef int8_t int8_t;
typedef int16_t int16_t;
typedef int32_t int32_t;
typedef int64_t int64_t;
typedef FILE FILE;

#define ECP_NB_CURVES 11
```

Generate the harness:

```bash
uv run python -m excido.fuzz_builder \
    -f mbedtls_x509_crt_parse_der \
    -c mbedtls/build/compile_commands.json \
    -s stubs_mbedtls.h
```

Check compilation:

```bash
cc -fsyntax-only -std=c11 fuzz.c
# 0 errors
```

The output is ~15,000 lines of self-contained C resolving 136 functions across 21 translation units.

## The stubs file

Each target needs a stubs file that:

1. **Provides libc headers** the harness needs (`<string.h>`, `<stdlib.h>`, etc.)
2. **Blacklists symbols** that shouldn't be resolved (any top-level symbol defined in the stubs file is added to the blacklist)
3. **Replaces platform-specific code** with host-compatible implementations

The `typedef size_t size_t;` pattern blacklists types. Clang parses the self-referential typedef without complaint and the blacklist parser sees `size_t` as a symbol defined in the stubs file. The resolver then skips any `size_t` typedef it encounters in the target's cross-compilation headers.

## How it works

1. `func_scanner` indexes all function definitions in the compile database
2. libclang parses the target function's translation unit
3. The dependency resolver walks the AST cursor tree, extracting source text for every referenced type, struct, enum, typedef, global variable, and called function
4. Unresolved functions are looked up in the func_scanner database and resolved from their source files (cross-TU resolution, runs in parallel)
5. Macros are resolved by scanning rendered source text for identifiers matching TU macro definitions
6. Symbols are topologically sorted and rendered: macros first, then types/globals, then functions
7. clang-format formats the output

See the [blog post](https://ian.goforth.systems/blog/excido) for a detailed walkthrough of every design decision.

## Project structure

```
src/excido/
    fuzz_builder.py     # main orchestrator
    dep_resolver.py     # AST-based dependency resolution
    dep_graph.py        # dependency graph with toposort
    db_ast.py           # Clang AST database, source extraction
    db_cc.py            # compile_commands.json wrapper
    db_func.py          # func_scanner database wrapper
    log.py              # structured JSONL logging
    templates.py        # AFL++ harness template
cxx/
    func_scanner.cpp    # C++ preprocessor-based function indexer
    fuzz_builder.cpp    # C++ port of the harness generator (WIP)
```

## License

MIT
