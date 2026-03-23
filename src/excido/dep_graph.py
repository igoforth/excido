from dataclasses import dataclass, field
from re import compile as re_compile, escape
from threading import Lock

from excido.clang import CursorKind, _CursorKind
from excido import log

SymbolKey = tuple[_CursorKind, str]  # (CursorKind, name)

@dataclass
class Symbol:
    """A single C symbol (type, function, or macro) to be emitted."""

    name: str
    kind: _CursorKind
    source: str  # C source text to emit
    deps: list[SymbolKey] = field(default_factory=list)
    size: int = -1  # byte size, populated for VAR_DECL globals


@dataclass
class DepGraph:
    """Dependency graph of symbols. Supports topological ordering."""

    symbols: dict[SymbolKey, Symbol] = field(default_factory=dict)
    blacklist: set[str] = field(default_factory=set)
    _lock: Lock = field(default_factory=Lock, repr=False)

    @staticmethod
    def _key(sym: Symbol) -> SymbolKey:
        return (sym.kind, sym.name)

    def add(self, sym: Symbol) -> None:
        if sym.name in self.blacklist:
            log.debug("graph_blacklist_skip", f"Blacklisted, skipping: {sym.name}", symbol=sym.name)
            return
        key = self._key(sym)
        with self._lock:
            if key not in self.symbols:
                self.symbols[key] = sym

    def has(self, kind: _CursorKind, name: str) -> bool:
        with self._lock:
            return (kind, name) in self.symbols

    def toposort(self) -> list[Symbol]:
        """Kahn's algorithm — returns symbols in definition order.

        Symbols with no dependencies come first. If a cycle exists,
        remaining symbols are appended at the end (best-effort).
        Thread-safe: takes a snapshot of symbols under the lock.
        """
        with self._lock:
            snapshot = dict(self.symbols)

        in_degree: dict[SymbolKey, int] = {k: 0 for k in snapshot}
        adj: dict[SymbolKey, list[SymbolKey]] = {k: [] for k in snapshot}
        for key, sym in snapshot.items():
            seen_deps: set[SymbolKey] = set()
            for dep_key in sym.deps:
                # skip self-loops and duplicates
                if dep_key == key or dep_key not in snapshot or dep_key in seen_deps:
                    continue
                seen_deps.add(dep_key)
                adj[dep_key].append(key)
                in_degree[key] += 1

        queue = [k for k, deg in in_degree.items() if deg == 0]
        result: list[Symbol] = []
        while queue:
            key = queue.pop(0)
            result.append(snapshot[key])
            for dependent in adj[key]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        # handle cycle nodes: break cycles by picking lowest in-degree nodes
        visited = {self._key(s) for s in result}
        remaining = {k: sym for k, sym in snapshot.items() if k not in visited}
        if remaining:
            cycle_names = [f"{k[1]} ({k[0]})" for k in remaining]
            log.warning("toposort_cycle", f"Toposort: {len(remaining)} nodes in cycles, breaking cycles: {', '.join(cycle_names)}", count=len(remaining), symbols=cycle_names)
            rem_in: dict[SymbolKey, int] = {k: 0 for k in remaining}
            rem_adj: dict[SymbolKey, list[SymbolKey]] = {k: [] for k in remaining}
            for key, sym in remaining.items():
                seen_deps: set[SymbolKey] = set()
                for dep_key in sym.deps:
                    if dep_key == key or dep_key not in remaining or dep_key in seen_deps:
                        continue
                    seen_deps.add(dep_key)
                    rem_adj[dep_key].append(key)
                    rem_in[key] += 1
            while rem_in:
                queue2 = [k for k, deg in rem_in.items() if deg == 0]
                if not queue2:
                    # true cycle — break it by picking lowest in-degree node
                    min_deg = min(rem_in.values())
                    queue2 = [k for k, deg in rem_in.items() if deg == min_deg][:1]
                while queue2:
                    key = queue2.pop(0)
                    if key in rem_in:
                        result.append(remaining[key])
                        del rem_in[key]
                        for dependent in rem_adj.get(key, []):
                            if dependent in rem_in:
                                rem_in[dependent] -= 1
                                if rem_in[dependent] == 0:
                                    queue2.append(dependent)

        return result

    @staticmethod
    def _ensure_semicolon(source: str) -> str:
        """Ensure a declaration ends with a semicolon.

        Cursor extents for typedefs, structs, enums, and globals often
        exclude the trailing semicolon — append one if missing.
        """
        stripped = source.rstrip()
        if stripped and not stripped.endswith(";"):
            return stripped + ";"
        return source

    def render_source(self) -> str:
        """Fast render: concatenate all symbol sources for tokenization.

        No toposort, no dedup — just raw source text. Used by macro
        resolution where ordering doesn't matter.
        """
        with self._lock:
            return "\n".join(s.source for s in self.symbols.values() if s.source.strip())

    def render(self) -> str:
        """Emit all symbols in topological order as C source.

        Order: macros, then types/enums/structs/globals, then functions.
        Within each group the toposort order is preserved.
        Skips struct/enum definitions that are already physically
        contained in another symbol's source.
        """
        ordered = self.toposort()
        macro_parts: list[str] = []
        type_parts: list[str] = []
        fn_parts: list[str] = []

        # pre-scan: collect all non-empty sources to detect
        # standalone structs that are nested inside another symbol
        all_other_source: dict[SymbolKey, str] = {}
        for sym in ordered:
            if sym.source.strip():
                all_other_source[self._key(sym)] = sym.source

        for sym in ordered:
            if not sym.source.strip():
                continue
            if sym.kind == CursorKind.MACRO_DEFINITION:
                macro_parts.append(sym.source)
            elif sym.kind in (CursorKind.FUNCTION_DECL, CursorKind.CXX_METHOD):
                fn_parts.append(sym.source)
            else:
                # skip standalone struct/enum if its definition is
                # physically nested inside another symbol's source
                if sym.kind in (CursorKind.STRUCT_DECL, CursorKind.ENUM_DECL):
                    my_key = self._key(sym)
                    pattern = re_compile(rf"\bstruct\s+{escape(sym.name)}\s*\{{")
                    found_in_other = False
                    for other_key, other_src in all_other_source.items():
                        if other_key == my_key:
                            continue
                        if pattern.search(other_src):
                            found_in_other = True
                            break
                    if found_in_other:
                        log.debug("render_skip_nested", f"Skipping duplicate nested struct: {sym.name}", symbol=sym.name)
                        continue
                type_parts.append(self._ensure_semicolon(sym.source))
        return "\n\n".join(macro_parts + type_parts + fn_parts)
