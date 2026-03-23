from pathlib import Path

UTILS_PATH = Path("utils")
CACHE_PATH = UTILS_PATH / "cache"

THREADS = 8
FUNC_C_DB = Path("c.db")

# Bare primitive types that never have a typedef in the TU.
# Checked after qualifier/pointer stripping in find_cursor.
PRIMITIVE_TYPES: set[str] = {
    "void", "char", "short", "int", "long", "float", "double", "bool",
}
