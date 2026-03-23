# Import clang python
# if str(CLANG_PYTHON) not in sys.path:
#     sys.path.insert(0, str(CLANG_PYTHON))

from typing import Any

from clang.cindex import (
    CompilationDatabase,
    CompileCommand,
    CompileCommands,
    Config,
    Cursor,
    TranslationUnit,
)
from clang.cindex import (
    CursorKind as _CursorKind,
)

# CursorKind attributes (FUNCTION_DECL, FIELD_DECL, etc.) are registered
# dynamically at module level, so pyright can't see them. Cast to Any to
# suppress reportAttributeAccessIssue.
CursorKind: Any = _CursorKind

# Config.set_library_path(str(LLVM_BASE / "lib"))
Config.set_library_file("/usr/lib/llvm-23/lib/libclang-23.so")
