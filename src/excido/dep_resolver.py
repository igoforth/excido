from __future__ import annotations

from re import escape, findall, search

from excido.clang import Cursor, CursorKind
from excido.db_ast import AstDatabase
from excido.dep_graph import DepGraph, Symbol, SymbolKey
from excido import log
from excido.log import ResolveContext


class DependencyResolver:
    def __init__(self, a_db: "AstDatabase", blacklist: set[str] | None = None) -> None:
        self.a_db = a_db
        self.unresolved_functions: set[str] = set()
        # extent ranges of macro instantiations that expand through blacklisted
        # macros — used to skip CALL_EXPRs inside expansions while still
        # allowing the wrapper macro's #define to be emitted
        self._blacklisted_macro_ranges: list[tuple[int, int]] = []
        if blacklist:
            self._blacklisted_macro_ranges = self._collect_blacklisted_ranges(
                a_db, blacklist
            )
            if self._blacklisted_macro_ranges:
                log.debug("blacklist_ranges", f"Collected {len(self._blacklisted_macro_ranges)} blacklisted macro ranges", count=len(self._blacklisted_macro_ranges))

    @staticmethod
    def _collect_blacklisted_ranges(
        a_db: "AstDatabase", blacklist: set[str]
    ) -> list[tuple[int, int]]:
        """Collect MACRO_INSTANTIATION offset ranges for macros that expand
        through any blacklisted macro, transitively.

        A macro like GNSS_ASSERT whose definition references ASSERT (blacklisted)
        gets its instantiation ranges collected so CALL_EXPRs inside the
        expansion are skipped. The macro's #define is NOT suppressed — only
        the expanded function calls are.
        """
        macro_defs = a_db.macro_defs

        # build name -> set of identifiers referenced in the definition
        def_tokens: dict[str, set[str]] = {}
        for name, defn in macro_defs.items():
            if not defn.location.file:
                continue
            src = a_db.get_content_from_ast_api(defn)
            if src:
                def_tokens[name] = set(findall(r"[A-Za-z_]\w*", src))

        # find all macro names whose definitions transitively reference
        # a blacklisted macro (but don't add them to the symbol blacklist)
        suppressed: set[str] = set(blacklist)
        changed = True
        while changed:
            changed = False
            for name, tokens in def_tokens.items():
                if name in suppressed:
                    continue
                if tokens & suppressed:
                    suppressed.add(name)
                    changed = True

        # collect instantiation ranges for all suppressed macros
        ranges: list[tuple[int, int]] = []
        for c in a_db.tlu.cursor.get_children():
            if c.kind == CursorKind.MACRO_INSTANTIATION and c.spelling in suppressed:
                ranges.append((c.extent.start.offset, c.extent.end.offset))
        return ranges

    # ------------------------------------------------------------------
    # Global variable resolution
    # ------------------------------------------------------------------

    def _resolve_global(self, ref: Cursor, graph: "DepGraph", ctx: ResolveContext) -> SymbolKey | None:
        """Add a TU-scope global variable declaration to the graph and resolve its type deps."""
        key: SymbolKey = (CursorKind.VAR_DECL, ref.spelling)
        if graph.has(*key):
            return key
        if ref.spelling in (graph.blacklist or set()):
            return None

        source = self.a_db.get_content_from_ast_api(ref)
        if not source or not source.strip():
            log.warning("global_source_fail", f"Could not extract source for global {ref.spelling}", symbol=ref.spelling, chain=ctx.chain)
            return None

        # strip 'extern' only if the underlying type has a complete
        # definition in the graph — otherwise keep extern for linking
        type_size = ref.type.get_size()
        type_decl = ref.type.get_declaration()
        type_complete_in_graph = False
        if type_decl and type_decl.spelling:
            # check if the struct/typedef body is in the graph with source
            for kind in (CursorKind.STRUCT_DECL, CursorKind.TYPEDEF_DECL):
                gkey: SymbolKey = (kind, type_decl.spelling)
                if graph.has(*gkey):
                    gsym = graph.symbols[gkey]
                    if gsym.source.strip() and "{" in gsym.source:
                        type_complete_in_graph = True
                        break
        if type_complete_in_graph:
            source = source.replace("extern ", "", 1)

        sym = Symbol(name=ref.spelling, kind=CursorKind.VAR_DECL, source=source, size=type_size)
        graph.add(sym)
        log.event("global_resolved", f"Resolved global: {ref.spelling} ({ref.type.spelling}, {type_size} bytes)", symbol=ref.spelling, type=ref.type.spelling, size=type_size, chain=ctx.chain)

        # resolve the type of this global (unwrap pointers/arrays to get element type)
        dep_keys: list[SymbolKey] = []
        var_type = ref.type
        while var_type.get_pointee().spelling:
            var_type = var_type.get_pointee()
        if var_type.kind.name == "CONSTANTARRAY":
            var_type = var_type.element_type
        type_decl = var_type.get_declaration()
        if type_decl and type_decl.spelling and type_decl.kind != CursorKind.NO_DECL_FOUND:
            type_def = type_decl.get_definition()
            if type_def and type_def.is_definition():
                type_decl = type_def
            # promote bare struct/enum to wrapping typedef
            if type_decl.kind in (CursorKind.STRUCT_DECL, CursorKind.ENUM_DECL):
                target_hash = type_decl.hash
                for tc in self.a_db.tlu.cursor.get_children():
                    if tc.kind == CursorKind.TYPEDEF_DECL:
                        inner = tc.underlying_typedef_type.get_declaration()
                        if inner and inner.hash == target_hash:
                            type_decl = tc
                            break
            type_key: SymbolKey = (type_decl.kind, type_decl.spelling)
            if not graph.has(*type_key):
                type_source = self.a_db.get_content_from_ast_api(type_decl)
                if type_source and type_source.strip():
                    type_sym = Symbol(name=type_decl.spelling, kind=type_decl.kind, source=type_source)
                    graph.add(type_sym)
                    # if this is a typedef wrapping a forward-declared struct,
                    # resolve the struct body
                    if type_decl.kind == CursorKind.TYPEDEF_DECL:
                        td_inner = type_decl.underlying_typedef_type.get_declaration()
                        if td_inner and td_inner.spelling and td_inner.kind in (CursorKind.STRUCT_DECL, CursorKind.ENUM_DECL):
                            td_inner_key: SymbolKey = (td_inner.kind, td_inner.spelling)
                            if not graph.has(*td_inner_key):
                                td_inner_def = td_inner if td_inner.is_definition() else td_inner.get_definition()
                                typedef_contains_body = (
                                    td_inner_def and td_inner_def.is_definition()
                                    and td_inner_def.extent.start.offset >= type_decl.extent.start.offset
                                    and td_inner_def.extent.end.offset <= type_decl.extent.end.offset
                                )
                                if typedef_contains_body:
                                    graph.add(Symbol(name=td_inner.spelling, kind=td_inner.kind, source=""))
                                elif td_inner_def and td_inner_def.is_definition():
                                    td_inner_source = self.a_db.get_content_from_ast_api(td_inner_def)
                                    if td_inner_source and td_inner_source.strip():
                                        td_inner_sym = Symbol(name=td_inner.spelling, kind=td_inner.kind, source=td_inner_source)
                                        graph.add(td_inner_sym)
                                        type_sym.deps.append(td_inner_key)
                                        td_inner_sym.deps = self.resolve_deps_graph(td_inner_def, graph, ctx.child(td_inner.spelling))
                                        log.event("global_type_fwd_struct", f"Global type: resolved forward-declared struct: {td_inner.spelling}", symbol=td_inner.spelling, chain=ctx.child(td_inner.spelling).chain)
                                    else:
                                        graph.add(Symbol(name=td_inner.spelling, kind=td_inner.kind, source=""))
                                else:
                                    graph.add(Symbol(name=td_inner.spelling, kind=td_inner.kind, source=""))
                    type_sym.deps = self.resolve_deps_graph(type_decl, graph, ctx.child(type_decl.spelling))
                    log.event("global_type_resolved", f"Resolved global type: {type_decl.spelling}", symbol=type_decl.spelling, chain=ctx.child(type_decl.spelling).chain)
            dep_keys.append(type_key)

        # walk the global's initializer to resolve referenced globals/symbols,
        # but only if the source contains an initializer (has '=')
        # and skip children whose location is outside the declaration's own
        # extent (macro expansions can leak unrelated code into the subtree)
        if "=" in source:
            init_start = ref.extent.start.line
            init_end = ref.extent.end.line
            for child in ref.walk_preorder():
                loc = child.location
                if loc.file and (loc.line < init_start or loc.line > init_end):
                    continue
                # reuse _process_cursor_graph via a mini sliding window
                cl: list[Cursor | None] = [None, child, None]
                key = self._process_cursor_graph(cl, graph, ctx.child(ref.spelling))
                if key:
                    dep_keys.append(key)
        sym.deps = dep_keys

        return key

    # ------------------------------------------------------------------
    # Graph-based dependency resolution
    # ------------------------------------------------------------------

    def resolve_deps_graph(self, cursor: Cursor, graph: "DepGraph", ctx: ResolveContext | None = None) -> list[SymbolKey]:
        """Walk cursor's subtree, find dependencies, add to graph.

        Returns the list of SymbolKeys that `cursor` depends on.
        The graph handles deduplication — if a symbol is already present
        we skip recursion but still return the key as a dependency edge.
        """
        if ctx is None:
            ctx = ResolveContext(chain=(cursor.spelling,))
        if ctx.depth <= 1:
            log.event("resolve_start", f"Resolving dependencies for {cursor.spelling}", symbol=cursor.spelling, chain=ctx.chain)
        else:
            log.debug("resolve_recurse", f"Resolving dependencies for {cursor.spelling}", symbol=cursor.spelling, chain=ctx.chain)
        dep_keys: list[SymbolKey] = []

        # 3-cursor sliding window: [prev, current, next]
        cl: list[Cursor | None] = [None, None, None]

        for next_cursor in cursor.walk_preorder():
            if cl[1] is None:
                cl[1] = next_cursor
                continue
            cl[2] = next_cursor

            key = self._process_cursor_graph(cl, graph, ctx)
            if key:
                dep_keys.append(key)

            cl[0] = cl[1]
            cl[1] = cl[2]

        # flush last cursor
        if cl[1] is not None:
            cl[2] = None
            key = self._process_cursor_graph(cl, graph, ctx)
            if key:
                dep_keys.append(key)

        if ctx.depth <= 1:
            log.event("resolve_done", f"Resolved {len(dep_keys)} deps for {cursor.spelling}", symbol=cursor.spelling, dep_count=len(dep_keys), chain=ctx.chain)

        return dep_keys

    def resolve_macros_graph(self, graph: "DepGraph") -> None:
        """Resolve all macros referenced by symbols in the graph.

        Builds an index of all MACRO_DEFINITION cursors in the TU, then
        scans graph source text for macro names and inlines their #define.
        Repeats until no new macros are found (transitive), but only
        re-tokenizes newly added macro sources on subsequent passes.
        """
        macro_defs = self.a_db.macro_defs
        log.event("macro_index", f"Macro definition index: {len(macro_defs)} macros in TU", count=len(macro_defs))

        # build initial token set from fast source concat (no toposort needed)
        all_source = graph.render_source()
        tokens = set(findall(r"[A-Za-z_]\w*", all_source))

        resolved: set[str] = set()
        while True:
            new_count = 0
            new_sources: list[str] = []

            for name, defn in macro_defs.items():
                if name in resolved:
                    continue
                if name not in tokens:
                    continue

                resolved.add(name)
                if name in graph.blacklist:
                    log.debug("macro_blacklisted", f"Macro {name} blacklisted, skipping", macro=name)
                    continue
                key: SymbolKey = (CursorKind.MACRO_DEFINITION, name)
                if graph.has(*key):
                    continue

                defn_file = defn.location.file
                if not defn_file:
                    log.debug("macro_dflag", f"Macro {name} defined via -D flag, skipping", macro=name)
                    continue

                source = self.a_db.get_content_from_ast_api(defn)
                if not source or not source.strip():
                    continue

                if not source.lstrip().startswith("#define"):
                    source = f"#define {source}"

                sym = Symbol(name=name, kind=CursorKind.MACRO_DEFINITION, source=source)
                graph.add(sym)
                new_count += 1
                new_sources.append(source)
                log.event("macro_resolved", f"Resolved macro: {name} from {defn_file.name}:{defn.location.line}", macro=name, file=defn_file.name, line=defn.location.line)

            if new_count == 0:
                break
            # only tokenize new macro sources for the next pass
            for src in new_sources:
                tokens.update(findall(r"[A-Za-z_]\w*", src))
            log.event("macro_pass", f"Resolved {new_count} new macros, checking for transitive deps", count=new_count)

        # resolve globals referenced in macro source text
        # (needed because VAR_DECL DECL_REF_EXPRs inside blacklisted macro
        # ranges are now blocked — globals like sm_message_as_id that are
        # referenced via non-blacklisted macros like SM_SUB need this path)
        # Build set of TU-scope global names from the cursor index (fast),
        # then intersect with macro tokens to avoid slow find_cursor lookups.
        self.a_db._build_cursor_index()
        assert self.a_db._cursor_index is not None
        tu_globals: dict[str, Cursor] = {}
        for (kind, name), cursors in self.a_db._cursor_index.items():
            if kind != CursorKind.VAR_DECL:
                continue
            for c in cursors:
                if (c.semantic_parent
                        and c.semantic_parent.kind == CursorKind.TRANSLATION_UNIT):
                    tu_globals[name] = c
                    break

        all_macro_source = "\n".join(
            sym.source for sym in graph.symbols.values()
            if sym.kind == CursorKind.MACRO_DEFINITION and sym.source.strip()
            and sym.name not in (graph.blacklist or set())
        )
        macro_idents = set(findall(r"[A-Za-z_]\w*", all_macro_source))
        # only resolve globals that appear in macro text AND exist in the TU
        candidates = macro_idents & set(tu_globals.keys())
        global_count = 0
        for name in candidates:
            var_key: SymbolKey = (CursorKind.VAR_DECL, name)
            if graph.has(*var_key) or name in (graph.blacklist or set()):
                continue
            macro_ctx = ResolveContext(chain=(name,))
            self._resolve_global(tu_globals[name], graph, macro_ctx)
            global_count += 1
        if global_count:
            log.event("macro_globals", f"Resolved {global_count} globals from macro source text", count=global_count)

    def resolve_sizeof_graph(self, graph: "DepGraph") -> None:
        """Scan graph source for sizeof(TypeName) and add dependency edges.

        When a symbol's source contains sizeof(T), T must be a complete type
        and must appear before the symbol in the output. This method finds
        such references and ensures the type's struct body is resolved and
        linked as a dependency.
        """
        all_source = graph.render_source()
        sizeof_refs = findall(r"\bsizeof\s*\(\s*([A-Za-z_]\w*)\s*\)", all_source)
        if not sizeof_refs:
            return

        new_count = 0
        for type_name in set(sizeof_refs):
            # find which symbol(s) use sizeof(type_name)
            # and ensure the type is complete in the graph
            type_key_typedef: SymbolKey = (CursorKind.TYPEDEF_DECL, type_name)
            type_key_struct: SymbolKey = (CursorKind.STRUCT_DECL, type_name)

            # check if this is a typedef that wraps a forward-declared struct
            if graph.has(*type_key_typedef):
                sym = graph.symbols[type_key_typedef]
                # if the typedef source is just a forward declaration,
                # the struct body should already be resolved by our
                # forward-decl fix — just need to add dep edges
                # Find the inner struct key
                inner_name = None
                inner_match = search(r"\bstruct\s+(\w+)", sym.source)
                if inner_match:
                    inner_name = inner_match.group(1)
                    inner_key: SymbolKey = (CursorKind.STRUCT_DECL, inner_name)
                    if graph.has(*inner_key):
                        inner_sym = graph.symbols[inner_key]
                        if not inner_sym.source.strip():
                            # empty alias — check if the typedef already
                            # contains the struct body inline before resolving
                            typedef_sym = graph.symbols[type_key_typedef]
                            if search(rf"\bstruct\s+{escape(inner_name)}\s*\{{", typedef_sym.source):
                                # body is inline in the typedef — leave alias empty
                                pass
                            else:
                                # truly forward-declared — resolve struct body
                                cursor = self.a_db.find_cursor(
                                    kind=CursorKind.STRUCT_DECL, term=inner_name, definition=True
                                )
                                if isinstance(cursor, list):
                                    cursor = cursor[0]
                                if cursor and cursor.is_definition():
                                    source = self.a_db.get_content_from_ast_api(cursor)
                                    if source and source.strip():
                                        inner_sym.source = source
                                        sizeof_ctx = ResolveContext(chain=("sizeof", type_name, inner_name))
                                        inner_sym.deps = self.resolve_deps_graph(cursor, graph, sizeof_ctx)
                                        log.event("sizeof_struct_resolved", f"sizeof: resolved struct body for {inner_name}", symbol=inner_name, chain=sizeof_ctx.chain)
                                        new_count += 1

                # add dep edges: any symbol containing sizeof(type_name)
                # must depend on the typedef AND the inner struct
                for sym_key, sym in graph.symbols.items():
                    if not sym.source or type_name not in sym.source:
                        continue
                    if sym_key == type_key_typedef or (inner_name and sym_key == (CursorKind.STRUCT_DECL, inner_name)):
                        continue
                    sizeof_pattern = rf"\bsizeof\s*\(\s*{escape(type_name)}\s*\)"
                    if not search(sizeof_pattern, sym.source):
                        continue
                    if type_key_typedef not in sym.deps:
                        sym.deps.append(type_key_typedef)
                    if inner_name:
                        inner_key = (CursorKind.STRUCT_DECL, inner_name)
                        if graph.has(*inner_key) and inner_key not in sym.deps:
                            sym.deps.append(inner_key)
                            new_count += 1

        if new_count > 0:
            log.event("sizeof_edges", f"sizeof: added {new_count} dependency edges", count=new_count)

    def _process_cursor_graph(
        self, cl: list[Cursor | None], graph: "DepGraph", ctx: ResolveContext
    ) -> SymbolKey | None:
        """Evaluate cl[1] as a potential dependency. Returns its SymbolKey if added."""
        if cl[1] is None:
            return None

        # log.debug(
        #     "cursor_visit",
        #     f"{cl[1].spelling} {cl[1].kind} "
        #     f"{cl[1].extent.start.line} - {cl[1].extent.end.line}",
        #     symbol=cl[1].spelling, kind=str(cl[1].kind),
        #     start_line=cl[1].extent.start.line, end_line=cl[1].extent.end.line,
        #     chain=ctx.chain,
        # )

        # --- kind filter ---
        if cl[1].kind not in [
            CursorKind.TYPE_REF,
            CursorKind.UNEXPOSED_EXPR,
            CursorKind.FIELD_DECL,
            CursorKind.CALL_EXPR,
            CursorKind.DECL_REF_EXPR,
        ]:
            return None

        if cl[1].spelling.strip() == "":
            return None

        # --- skip cursors inside blacklisted macro expansions ---
        # Blocks function calls, variable refs, and unexposed exprs from
        # trace/logging macros. Globals referenced by non-blacklisted macros
        # (like SM_SUB) are resolved via the macro source scanner instead.
        if self._blacklisted_macro_ranges and cl[1].kind in (
            CursorKind.CALL_EXPR, CursorKind.DECL_REF_EXPR, CursorKind.UNEXPOSED_EXPR
        ):
            off = cl[1].extent.start.offset
            for ms, me in self._blacklisted_macro_ranges:
                if ms <= off < me:
                    log.debug("macro_blacklist_skip", f"Skipping {cl[1].kind} {cl[1].spelling} inside blacklisted macro", symbol=cl[1].spelling, kind=str(cl[1].kind), chain=ctx.chain)
                    return None

        # --- DECL_REF_EXPR: keep enum constants; resolve TU-scope globals and function refs ---
        if cl[1].kind == CursorKind.DECL_REF_EXPR:
            ref_def = cl[1].get_definition()
            if not ref_def:
                ref = cl[1].referenced
                if not ref:
                    return None
                if (ref.kind == CursorKind.VAR_DECL
                        and ref.semantic_parent
                        and ref.semantic_parent.kind == CursorKind.TRANSLATION_UNIT):
                    return self._resolve_global(ref, graph, ctx.child(ref.spelling))
                # function pointer ref with no visible definition — mark for cross-TU
                if ref.kind == CursorKind.FUNCTION_DECL and ref.spelling:
                    log.warning(
                        "unresolved_func",
                        f"Could not find definition of {ref.spelling}, "
                        "may need another TranslationUnit",
                        symbol=ref.spelling, chain=ctx.chain,
                    )
                    self.unresolved_functions.add(ref.spelling)
                    return (CursorKind.FUNCTION_DECL, ref.spelling)
                return None
            # resolve TU-scope globals (static const arrays, file-scope vars)
            if ref_def.kind == CursorKind.VAR_DECL:
                if (ref_def.semantic_parent
                        and ref_def.semantic_parent.kind == CursorKind.TRANSLATION_UNIT):
                    return self._resolve_global(ref_def, graph, ctx.child(ref_def.spelling))
                return None
            # function pointer ref to a function with a definition in this TU
            if ref_def.kind == CursorKind.FUNCTION_DECL:
                key = (CursorKind.FUNCTION_DECL, ref_def.spelling)
                if graph.has(*key):
                    return key
                if not ref_def.is_definition():
                    # only a declaration — mark for cross-TU resolution
                    self.unresolved_functions.add(ref_def.spelling)
                    return key
                source = self.a_db.get_content_from_ast_api(ref_def)
                if source and source.strip():
                    sym = Symbol(name=ref_def.spelling, kind=CursorKind.FUNCTION_DECL, source=source)
                    graph.add(sym)
                    child_ctx = ctx.child(ref_def.spelling)
                    sym.deps = self.resolve_deps_graph(ref_def, graph, child_ctx)
                    log.event("func_ref_resolved", f"Resolved function ref: {ref_def.spelling}", symbol=ref_def.spelling, chain=child_ctx.chain)
                    return key
                return None
            if ref_def.kind != CursorKind.ENUM_CONSTANT_DECL:
                return None

        # --- UNEXPOSED_EXPR: discard vars/params and adjacent duplicates ---
        if cl[1].kind == CursorKind.UNEXPOSED_EXPR:
            cl_def = cl[1].get_definition()
            if not cl_def:
                ref = cl[1].referenced
                if (ref and ref.kind == CursorKind.VAR_DECL
                        and ref.semantic_parent
                        and ref.semantic_parent.kind == CursorKind.TRANSLATION_UNIT):
                    return self._resolve_global(ref, graph, ctx.child(ref.spelling))
                return None
            if cl_def.kind in [
                CursorKind.VAR_DECL,
                CursorKind.PARM_DECL,
            ]:
                return None

            # skip adjacent duplicates, but not enum constants
            # (enum constants appear as UNEXPOSED_EXPR + DECL_REF_EXPR pairs)
            if cl_def.kind != CursorKind.ENUM_CONSTANT_DECL:
                for adj_kind in [CursorKind.CALL_EXPR, CursorKind.DECL_REF_EXPR]:
                    if cl[0] and cl[2] and (
                        (cl[0].kind == adj_kind and cl[0].spelling == cl[1].spelling)
                        or (cl[2].kind == adj_kind and cl[2].spelling == cl[1].spelling)
                    ):
                        return None

        # --- get definition ---
        c_def: Cursor | None = cl[1].get_definition()
        if not c_def or not c_def.is_definition():
            if c_def is None:
                log.warning(
                    "no_definition",
                    f"Could not find definition of {cl[1].spelling}, "
                    "may need another TranslationUnit",
                    symbol=cl[1].spelling, kind=str(cl[1].kind), chain=ctx.chain,
                )
                if cl[1].kind == CursorKind.CALL_EXPR and cl[1].spelling:
                    self.unresolved_functions.add(cl[1].spelling)
                    # return a prospective key so the dep edge exists
                    # when cross-TU resolution later adds the symbol
                    return (CursorKind.FUNCTION_DECL, cl[1].spelling)
            return None

        # log.debug("def_found", f"Found {cl[1].spelling} with definition {c_def.spelling}", symbol=cl[1].spelling, definition=c_def.spelling, chain=ctx.chain)

        # never emit function parameters as top-level symbols
        if c_def.kind == CursorKind.PARM_DECL:
            return None

        # if this struct/enum has a wrapping typedef in the TU, promote to it
        # so we emit `typedef struct X { ... } X_type;` instead of a bare
        # `struct X { ... }` — clang reports semantic_parent as TU even when
        # the struct is physically inside a typedef, so we scan TU children
        if c_def.kind in (CursorKind.STRUCT_DECL, CursorKind.ENUM_DECL):
            target_hash = c_def.hash
            for tc in self.a_db.tlu.cursor.get_children():
                if tc.kind == CursorKind.TYPEDEF_DECL:
                    inner = tc.underlying_typedef_type.get_declaration()
                    if inner and inner.hash == target_hash:
                        log.debug("promote_typedef", f"Promoting {c_def.kind} '{c_def.spelling}' to TYPEDEF '{tc.spelling}'", from_symbol=c_def.spelling, to_symbol=tc.spelling, chain=ctx.chain)
                        c_def = tc
                        break

        key: SymbolKey = (c_def.kind, c_def.spelling)
        if graph.has(*key):
            return key
        if c_def.spelling in (graph.blacklist or set()):
            return None

        # --- FIELD_DECL: resolve the underlying type declaration ---
        # Check c_def.kind (not cl[1].kind) because the reference cursor may
        # be a different kind (e.g. UNEXPOSED_EXPR) that resolves to a field.
        if c_def.kind == CursorKind.FIELD_DECL:
            # Unwrap pointers/arrays to get the underlying type, then ask
            # for its declaration cursor — no string manipulation needed.
            underlying = c_def.type
            while underlying.get_pointee().spelling:
                underlying = underlying.get_pointee()
            if underlying.kind.name == "CONSTANTARRAY":
                underlying = underlying.element_type

            type_decl = underlying.get_declaration()
            if not type_decl or type_decl.kind == CursorKind.NO_DECL_FOUND:
                # canonical type may resolve where the direct type didn't
                canonical = underlying.get_canonical()
                type_decl = canonical.get_declaration()
                if not type_decl or type_decl.kind == CursorKind.NO_DECL_FOUND:
                    return None

            # prefer the definition over a forward declaration
            type_def = type_decl.get_definition()
            if type_def and type_def.is_definition():
                type_decl = type_def

            # Anonymous struct/union types are defined inline in their
            # parent — the parent's source already contains them.
            if not type_decl.spelling or type_decl.is_anonymous():
                return None

            # promote bare struct/enum to wrapping typedef if one exists
            if type_decl.kind in (CursorKind.STRUCT_DECL, CursorKind.ENUM_DECL):
                target_hash = type_decl.hash
                for tc in self.a_db.tlu.cursor.get_children():
                    if tc.kind == CursorKind.TYPEDEF_DECL:
                        inner = tc.underlying_typedef_type.get_declaration()
                        if inner and inner.hash == target_hash:
                            log.debug("field_promote_typedef", f"FIELD_DECL: promoting {type_decl.kind} '{type_decl.spelling}' to TYPEDEF '{tc.spelling}'", from_symbol=type_decl.spelling, to_symbol=tc.spelling, chain=ctx.chain)
                            type_decl = tc
                            break

            field_key: SymbolKey = (type_decl.kind, type_decl.spelling)
            if graph.has(*field_key):
                return field_key

            source = self.a_db.get_content_from_ast_api(type_decl)
            if not source or not source.strip():
                return None

            sym = Symbol(name=type_decl.spelling, kind=type_decl.kind, source=source)
            graph.add(sym)
            child_ctx = ctx.child(type_decl.spelling)
            # if this is a typedef wrapping a named struct/enum, resolve
            # the inner type — emit full definition if forward-declared,
            # or register an empty alias if already defined
            if type_decl.kind == CursorKind.TYPEDEF_DECL:
                inner = type_decl.underlying_typedef_type.get_declaration()
                if inner and inner.spelling and inner.kind in (CursorKind.STRUCT_DECL, CursorKind.ENUM_DECL):
                    fld_inner_key: SymbolKey = (inner.kind, inner.spelling)
                    if not graph.has(*fld_inner_key):
                        inner_def = inner if inner.is_definition() else inner.get_definition()
                        typedef_contains_body = (
                            inner_def and inner_def.is_definition()
                            and inner_def.extent.start.offset >= type_decl.extent.start.offset
                            and inner_def.extent.end.offset <= type_decl.extent.end.offset
                        )
                        if typedef_contains_body:
                            graph.add(Symbol(name=inner.spelling, kind=inner.kind, source=""))
                        elif inner_def and inner_def.is_definition():
                            inner_source = self.a_db.get_content_from_ast_api(inner_def)
                            if inner_source and inner_source.strip():
                                inner_sym = Symbol(name=inner.spelling, kind=inner.kind, source=inner_source)
                                graph.add(inner_sym)
                                sym.deps.append(fld_inner_key)
                                inner_ctx = child_ctx.child(inner.spelling)
                                inner_sym.deps = self.resolve_deps_graph(inner_def, graph, inner_ctx)
                                log.event("field_fwd_struct", f"FIELD_DECL: resolved forward-declared struct: {inner.spelling}", symbol=inner.spelling, chain=inner_ctx.chain)
                            else:
                                graph.add(Symbol(name=inner.spelling, kind=inner.kind, source=""))
                        else:
                            graph.add(Symbol(name=inner.spelling, kind=inner.kind, source=""))
            sym.deps = self.resolve_deps_graph(type_decl, graph, child_ctx)
            return field_key

        # --- ENUM_CONSTANT_DECL: resolve the parent enum typedef ---
        if c_def.kind == CursorKind.ENUM_CONSTANT_DECL:
            parent = c_def.semantic_parent
            if parent and parent.kind == CursorKind.ENUM_DECL:
                # look for a TYPEDEF_DECL whose underlying type resolves to this enum
                enum_hash = parent.hash
                for tc in self.a_db.tlu.cursor.get_children():
                    if tc.kind == CursorKind.TYPEDEF_DECL:
                        decl = tc.underlying_typedef_type.get_declaration()
                        if decl and decl.hash == enum_hash:
                            parent = tc
                            break
                parent_key: SymbolKey = (parent.kind, parent.spelling or c_def.spelling)
                if graph.has(*parent_key):
                    return parent_key
                # for anonymous enums from different TUs, check if any of
                # the enum's constants already exist in a graph enum
                if parent.is_anonymous():
                    constants = [
                        ch.spelling for ch in parent.get_children()
                        if ch.kind == CursorKind.ENUM_CONSTANT_DECL and ch.spelling
                    ]
                    for sym in graph.symbols.values():
                        if sym.kind not in (CursorKind.ENUM_DECL, CursorKind.TYPEDEF_DECL):
                            continue
                        if any(search(rf"\b{escape(c)}\b", sym.source) for c in constants):
                            return (sym.kind, sym.name)
                source = self.a_db.get_content_from_ast_api(parent)
                if source and source.strip():
                    sym = Symbol(name=parent.spelling or c_def.spelling, kind=parent.kind, source=source)
                    graph.add(sym)
                    log.event("enum_resolved", f"Resolved enum: {sym.name} (via {c_def.spelling})", symbol=sym.name, via=c_def.spelling, chain=ctx.chain)
                    return parent_key
            return None

        # --- emit the definition ---
        source = self.a_db.get_content_from_ast_api(c_def)
        if not source or not source.strip():
            return None
        sym = Symbol(name=c_def.spelling, kind=c_def.kind, source=source)
        graph.add(sym)
        log.event("dep_resolved", f"Resolved {c_def.spelling} ({c_def.kind}) via {cl[1].kind}", symbol=c_def.spelling, def_kind=str(c_def.kind), ref_kind=str(cl[1].kind), chain=ctx.chain)

        # if this is a TYPEDEF_DECL wrapping a struct/enum, handle the
        # underlying type: emit the full definition if the typedef only
        # has a forward declaration, otherwise register an empty alias
        # so the struct isn't emitted separately
        child_ctx = ctx.child(c_def.spelling)
        if c_def.kind == CursorKind.TYPEDEF_DECL:
            inner = c_def.underlying_typedef_type.get_declaration()
            if inner and inner.spelling and inner.kind in (CursorKind.STRUCT_DECL, CursorKind.ENUM_DECL):
                inner_key: SymbolKey = (inner.kind, inner.spelling)
                if not graph.has(*inner_key):
                    inner_def = inner if inner.is_definition() else inner.get_definition()
                    # check if the struct body is physically inside the typedef extent
                    # (e.g. `typedef struct foo { ... } foo_t;`) — if so, just alias it
                    typedef_contains_body = (
                        inner_def and inner_def.is_definition()
                        and inner_def.extent.start.offset >= c_def.extent.start.offset
                        and inner_def.extent.end.offset <= c_def.extent.end.offset
                    )
                    if typedef_contains_body:
                        # struct body is inline in the typedef — register empty alias
                        graph.add(Symbol(name=inner.spelling, kind=inner.kind, source=""))
                    elif inner_def and inner_def.is_definition():
                        # forward-declared typedef — emit the struct body separately
                        inner_source = self.a_db.get_content_from_ast_api(inner_def)
                        if inner_source and inner_source.strip():
                            inner_sym = Symbol(name=inner.spelling, kind=inner.kind, source=inner_source)
                            graph.add(inner_sym)
                            sym.deps.append(inner_key)
                            inner_ctx = child_ctx.child(inner.spelling)
                            inner_sym.deps = self.resolve_deps_graph(inner_def, graph, inner_ctx)
                            log.event("fwd_struct_resolved", f"Resolved forward-declared struct: {inner.spelling}", symbol=inner.spelling, chain=inner_ctx.chain)
                        else:
                            graph.add(Symbol(name=inner.spelling, kind=inner.kind, source=""))
                    else:
                        graph.add(Symbol(name=inner.spelling, kind=inner.kind, source=""))

        # recurse into this definition's own deps
        sym.deps = self.resolve_deps_graph(c_def, graph, child_ctx)
        return key
