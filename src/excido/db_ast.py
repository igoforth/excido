from json import JSONDecoder
from pathlib import Path
from re import compile as re_compile, sub
from threading import RLock
from typing import Any

from excido.clang import (
    Cursor,
    CursorKind,
    TranslationUnit,
    _CursorKind,
)
from excido.constants import PRIMITIVE_TYPES
from excido.db_cc import CompileDB
from excido import log
from excido.utils import read_file_bytes

# Preprocessor directive patterns (compiled once)
_PP_IF_RE = re_compile(rb"^\s*#\s*(?:if|ifdef|ifndef)\b")
_PP_ELIF_ELSE_RE = re_compile(rb"^\s*#\s*(?:elif|else)\b")
_PP_ENDIF_RE = re_compile(rb"^\s*#\s*endif\b")


class AstDatabase:
    def __init__(self, compile_db: CompileDB, source_path: Path) -> None:
        self.source_path: Path = source_path.resolve()
        self.compile_db: CompileDB = compile_db
        self._lock: RLock = RLock()

    @property
    def ast(self) -> list[dict]:
        """Parsed JSON AST nodes, lazy-loaded from the compile database's AST dump."""
        with self._lock:
            if not hasattr(self, "_ast"):
                ast_path = self.compile_db.create_ast(self.source_path)
                if not ast_path:
                    log.error("ast_create_fail", f"Failed to create AST for {self.source_path}", path=str(self.source_path))
                    from sys import exit
                    exit(1)
                self._ast = self._parse_ast_json(ast_path)
            return self._ast

    @property
    def tlu(self) -> TranslationUnit:
        with self._lock:
            if not hasattr(self, "_tlu"):
                self._tlu = self.compile_db.create_translation_unit(self.source_path)
            if not self._tlu:
                log.error("tu_create_fail", f"Failed to create translation unit for {self.source_path}", path=str(self.source_path))
                from sys import exit
                exit(1)
            return self._tlu

    @property
    def macro_defs(self) -> dict[str, Cursor]:
        """Index of name -> MACRO_DEFINITION cursor from the TU's top-level children."""
        with self._lock:
            if not hasattr(self, "_macro_defs"):
                self._macro_defs: dict[str, Cursor] = {}
                for c in self.tlu.cursor.get_children():
                    if c.kind == CursorKind.MACRO_DEFINITION and c.spelling:
                        self._macro_defs[c.spelling] = c
                log.debug("macro_index_built", f"Built macro definition index with {len(self._macro_defs)} entries", count=len(self._macro_defs))
            return self._macro_defs

    @staticmethod
    def _parse_ast_json(file_path: Path) -> list[dict]:
        """Parse a clang JSON AST dump (possibly multiple top-level objects)."""
        with open(file_path.resolve(), "r") as f:
            content = f.read().strip()

        decoder = JSONDecoder()
        pos = 0
        nodes: list[dict] = []
        while pos < len(content):
            while pos < len(content) and content[pos] in " \t\n\r":
                pos += 1
            if pos >= len(content):
                break
            obj, end_pos = decoder.raw_decode(content, pos)
            nodes.append(obj)
            pos = end_pos
        return nodes

    # get_content_from_ast_dump() is dead code — only called from commented-out
    # lines. It used the old text-based AST dump + inverse_kind_mapping + fccf.
    # The active code path uses get_content_from_ast_api() (libclang cursor extents).

    @staticmethod
    def _count_pp_balance(data: bytes) -> int:
        """Return the number of unmatched #endif directives in data.

        Positive means more #endif than #if — we need to prepend that many
        opening #if directives from before the extent.
        """
        depth = 0
        min_depth = 0
        for line in data.split(b"\n"):
            if _PP_IF_RE.match(line):
                depth += 1
            elif _PP_ENDIF_RE.match(line):
                depth -= 1
                if depth < min_depth:
                    min_depth = depth
        # min_depth is how far below zero we went — that many #if are missing
        return -min_depth

    @staticmethod
    def _find_opening_guards(file_bytes: bytes, start_offset: int, needed: int) -> bytes:
        """Scan backwards from start_offset to find `needed` unmatched #if/#ifdef/#ifndef lines.

        Also collects any #elif/#else lines that are part of the same
        conditional block (they sit between the #if and the cursor extent).
        Returns the bytes to prepend (including newlines).
        """
        # Split the preceding content into lines (preserving byte offsets)
        preceding = file_bytes[:start_offset]
        lines = preceding.split(b"\n")

        collected: list[bytes] = []
        depth = 0  # tracks nesting as we scan backwards
        found = 0

        for line in reversed(lines):
            if _PP_ENDIF_RE.match(line):
                # a nested #endif — need to skip its matching #if too
                depth += 1
            elif _PP_IF_RE.match(line):
                if depth > 0:
                    depth -= 1
                else:
                    collected.append(line)
                    found += 1
                    if found >= needed:
                        break
            elif _PP_ELIF_ELSE_RE.match(line):
                # #elif/#else between the #if and cursor — include them
                if depth == 0:
                    collected.append(line)

        collected.reverse()
        if collected:
            return b"\n".join(collected) + b"\n"
        return b""

    def get_content_from_ast_api(self, cursor: Cursor) -> str | None:
        # clang offsets are byte offsets — slice bytes then decode
        start_file = cursor.extent.start.file
        if not start_file:
            return None
        start_offset = int(cursor.extent.start.offset)  # type: ignore
        end_offset = int(cursor.extent.end.offset)  # type: ignore
        path = Path(start_file.name)
        if not path.is_absolute():
            cc = self.compile_db.get_compile_command(self.source_path)
            if cc:
                path = (Path(cc.directory) / path).resolve()
        result = read_file_bytes(path)
        if isinstance(result, Exception):
            log.error("file_read_fail", f"An error occurred while reading {path}: {result}", path=str(path))
            from sys import exit
            exit(1)
        raw = result[start_offset : end_offset + 1]

        # Check for unbalanced preprocessor guards and prepend missing #if lines
        needed = self._count_pp_balance(raw)
        if needed > 0:
            prefix = self._find_opening_guards(result, start_offset, needed)
            if prefix:
                raw = prefix + raw
                log.debug(
                    "pp_guard_prepend",
                    f"Prepended {needed} preprocessor guard(s) for "
                    f"{cursor.spelling or '(anonymous)'}",
                    count=needed, symbol=cursor.spelling or "(anonymous)",
                )

        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("latin-1")

    def print_cursor_info(self, cursor: Cursor, indent: int = 0):
        """Recursively prints information about a cursor and its children."""
        info = (
            f"{'  ' * indent}Kind: {cursor.kind} | "
            f"Spelling: {cursor.spelling} | "
            f"Location: {cursor.location} | "
            f"Type: {cursor.type.spelling} ({cursor.type.kind})"
        )
        log.debug("cursor_info", info, kind=str(cursor.kind), symbol=cursor.spelling)

        if cursor.kind == CursorKind.FUNCTION_DECL:
            for arg in cursor.get_arguments():
                log.debug("cursor_arg", f"{'  ' * (indent + 1)}- {arg.spelling}: {arg.type}", symbol=arg.spelling)

        for child in cursor.get_children():
            self.print_cursor_info(child, indent + 1)

    @staticmethod
    def _verify_kind(
        cursor: Cursor,
        **kwargs: _CursorKind | str | bool,
    ) -> bool:
        return cursor.kind == kwargs["kind"]

    @staticmethod
    def _verify_term(
        cursor: Cursor,
        **kwargs: _CursorKind | str | bool,
    ) -> bool:
        return cursor.spelling == kwargs["term"]


    _cursor_index: dict[tuple[Any, str], list[Cursor]] | None = None

    def _build_cursor_index(self) -> None:
        """Build a (kind, spelling) -> [Cursor] index from a single TU walk."""
        if self._cursor_index is not None:
            return
        index: dict[tuple[Any, str], list[Cursor]] = {}
        for cursor in self.tlu.cursor.walk_preorder():
            if cursor.spelling:
                key = (cursor.kind, cursor.spelling)
                if key not in index:
                    index[key] = []
                index[key].append(cursor)
        self._cursor_index = index
        log.debug("cursor_index_built", f"Built cursor index with {len(index)} entries", count=len(index))

    def find_cursor(
        self,
        kind: _CursorKind | None = None,
        term: str = "",
        definition: bool = False,
    ) -> Cursor | list[Cursor] | None:
        # fast path: exact kind + term match via index
        if kind and term and not definition:
            self._build_cursor_index()
            assert self._cursor_index is not None
            results = self._cursor_index.get((kind, term), [])
            if len(results) == 1:
                return results[0]
            elif len(results) > 1:
                return results
            # try stripping qualifiers/pointers to get the base type name
            stripped = sub(r" \*+$", "", term)
            stripped = sub(r"^(volatile |unsigned |const |signed |struct |enum )+", "", stripped)
            if stripped in PRIMITIVE_TYPES:
                return None
            if stripped != term:
                results = self._cursor_index.get((kind, stripped), [])
                if len(results) == 1:
                    return results[0]
                elif len(results) > 1:
                    return results
            log.debug("cursor_index_miss", f"Cursor index miss for ({kind}, {term}), falling back to full walk", kind=str(kind), term=term)

        # slow path: full TU walk with flexible filters
        results = []
        dispatch = []
        kwargs = {}
        if kind:
            dispatch.append(self._verify_kind)
            kwargs["kind"] = kind
        if term:
            dispatch.append(self._verify_term)
            kwargs["term"] = term

        for cursor in self.tlu.cursor.walk_preorder():
            valid = all([func(cursor, **kwargs) for func in dispatch])
            if valid:
                if definition and not cursor.is_definition():
                    continue
                results.append(cursor)

        if len(results) == 1:
            return results[0]
        elif len(results) > 1:
            return results
        else:
            return None
