from __future__ import annotations

from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from shutil import which
from subprocess import TimeoutExpired, run
from sys import exit
from tempfile import NamedTemporaryFile
from threading import Lock

from excido.clang import (
    Cursor,
    CursorKind,
    TranslationUnit,
)
from excido.constants import (
    CACHE_PATH,
    THREADS,
)
from excido.db_ast import AstDatabase
from excido.db_cc import CompileDB
from excido.db_func import FunctionDatabase
from excido.dep_graph import DepGraph, Symbol
from excido.dep_resolver import DependencyResolver
from excido import log
from excido.log import ResolveContext
from excido.templates import (
    cocci_context_template,
    cocci_report_template,
    fuzz_template,
)


def _parse_stub_blacklist(stubs_path: Path) -> set[str]:
    """Parse a stub file as a mini TU and return all top-level symbol names."""
    tu = TranslationUnit.from_source(
        str(stubs_path),
        options=TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
    )
    names: set[str] = set()
    resolved = stubs_path.resolve()
    for c in tu.cursor.get_children():
        if not c.spelling:
            continue
        # only collect symbols defined in the stub file itself (skip builtins and includes)
        if not c.location.file or Path(c.location.file.name).resolve() != resolved:
            continue
        names.add(c.spelling)
    log.event("stub_blacklist", f"Stub blacklist: {len(names)} symbols from {stubs_path.name}: {sorted(names)}", count=len(names), file=stubs_path.name)
    return names



def _find_callers(function_name: str, source_dir: Path) -> str:
    """Find callers of a function using Coccinelle (spatch) if available.

    Returns a C comment block listing callers with call site context,
    or empty string if spatch is not installed.
    """
    if not which("spatch"):
        log.debug("spatch_missing", "spatch not found, skipping caller discovery")
        return ""

    report_cocci = cocci_report_template.substitute(function=function_name)
    context_cocci = cocci_context_template.substitute(function=function_name)

    callers: list[dict[str, str]] = []
    context_diff = ""

    with NamedTemporaryFile(mode="w", suffix=".cocci", delete=False) as f:
        f.write(report_cocci)
        report_path = f.name

    with NamedTemporaryFile(mode="w", suffix=".cocci", delete=False) as f:
        f.write(context_cocci)
        context_path = f.name

    try:
        # Report mode: get caller names and locations
        result = run(
            [
                "spatch", "--very-quiet", "-D", "report",
                "--cocci-file", report_path,
                "--dir", str(source_dir),
                "--include-headers",
            ],
            capture_output=True, text=True, timeout=120,
        )
        for line in result.stdout.strip().splitlines():
            if line.startswith("CALLER:"):
                parts = line.split(":", 3)
                if len(parts) == 4:
                    callers.append({
                        "fn": parts[1],
                        "file": parts[2],
                        "line": parts[3],
                    })

        # Context mode: get call site code
        result = run(
            [
                "spatch", "--very-quiet", "-D", "context",
                "--cocci-file", context_path,
                "--dir", str(source_dir),
                "--include-headers",
            ],
            capture_output=True, text=True, timeout=120,
        )
        context_diff = result.stdout.strip()

    except TimeoutExpired:
        log.warning("spatch_timeout", f"spatch timed out while searching for callers of {function_name}", function=function_name)
    except FileNotFoundError:
        log.warning("spatch_vanished", "spatch binary disappeared during execution")
        return ""
    finally:
        Path(report_path).unlink(missing_ok=True)
        Path(context_path).unlink(missing_ok=True)

    if not callers:
        return ""

    # Build comment block
    lines = []
    lines.append("/*")
    lines.append(f" * Callers of {function_name}:")
    for c in callers:
        short_file = Path(c["file"]).name
        lines.append(f" *   {c['fn']}() — {short_file}:{c['line']}")
    lines.append(" *")

    if context_diff:
        lines.append(" * Call site context:")
        for diff_line in context_diff.splitlines():
            # Only include the interesting lines (- prefixed = the call site)
            if diff_line.startswith("-") and not diff_line.startswith("---"):
                lines.append(f" *   {diff_line}")
    lines.append(" */")

    return "\n".join(lines)


def _is_assignment(cursor: Cursor, a_db: AstDatabase) -> bool:
    """Check if a BINARY_OPERATOR or COMPOUND_ASSIGNMENT_OPERATOR is an assignment."""
    if cursor.kind == CursorKind.COMPOUND_ASSIGNMENT_OPERATOR:
        return True
    if cursor.kind != CursorKind.BINARY_OPERATOR:
        return False
    children = list(cursor.get_children())
    if len(children) != 2:
        return False
    lhs, rhs = children
    lhs_end = lhs.extent.end.offset
    rhs_start = rhs.extent.start.offset
    if lhs_end >= rhs_start:
        return False
    src = a_db.get_content_from_ast_api(cursor)
    if not src:
        return False
    cursor_start = cursor.extent.start.offset
    op_text = src[lhs_end - cursor_start:rhs_start - cursor_start].strip()
    return op_text == "="


def _count_deref_rw(
    func_cursor: Cursor, param_name: str, a_db: AstDatabase
) -> tuple[int, int]:
    """Count deref reads and writes for a parameter in a function body.

    Returns (deref_reads, deref_writes).
    Only counts actual assignments (=, +=, etc.) as writes.
    """
    reads = 0
    writes = 0
    deref_kinds = (CursorKind.MEMBER_REF_EXPR, CursorKind.ARRAY_SUBSCRIPT_EXPR)
    assignment_kinds = (CursorKind.BINARY_OPERATOR, CursorKind.COMPOUND_ASSIGNMENT_OPERATOR)

    for child in func_cursor.walk_preorder():
        if child.kind not in assignment_kinds:
            continue
        if child.kind == CursorKind.BINARY_OPERATOR and not _is_assignment(child, a_db):
            has_param_deref = False
            for c in child.walk_preorder():
                if c.kind == CursorKind.DECL_REF_EXPR and c.spelling == param_name:
                    has_param_deref = True
                    break
            if has_param_deref:
                if any(c.kind in deref_kinds for c in child.walk_preorder()):
                    reads += 1
            continue

        children = list(child.get_children())
        if len(children) != 2:
            continue
        lhs, rhs = children

        lhs_has_param = any(
            c.kind == CursorKind.DECL_REF_EXPR and c.spelling == param_name
            for c in lhs.walk_preorder()
        )
        lhs_has_deref = any(c.kind in deref_kinds for c in lhs.walk_preorder())
        if lhs_has_param and lhs_has_deref:
            writes += 1

        rhs_has_param = any(
            c.kind == CursorKind.DECL_REF_EXPR and c.spelling == param_name
            for c in rhs.walk_preorder()
        )
        rhs_has_deref = any(c.kind in deref_kinds for c in rhs.walk_preorder())
        if rhs_has_param and rhs_has_deref:
            reads += 1
    return reads, writes


def _find_param_mapping(
    call_cursor: Cursor, caller_params: dict[str, Cursor], callee_cursor: Cursor
) -> dict[str, str]:
    """Map caller param names to callee param names at a call site.

    Returns {callee_param_name: caller_param_name} for params that are
    passed through directly.
    """
    mapping: dict[str, str] = {}
    call_children = list(call_cursor.get_children())
    call_args = call_children[1:]
    callee_params = list(callee_cursor.get_arguments())

    for i, callee_param in enumerate(callee_params):
        if i >= len(call_args):
            break
        arg_expr = call_args[i]
        for c in arg_expr.walk_preorder():
            if c.kind == CursorKind.DECL_REF_EXPR and c.spelling in caller_params:
                mapping[callee_param.spelling] = c.spelling
                break
    return mapping


def _collect_rw_counts(
    cursor: Cursor, graph: DepGraph, a_db: AstDatabase
) -> dict[str, tuple[int, int]]:
    """Collect read/write counts for pointer params across the call graph.

    Returns {param_name: (reads, writes)} for pointer params only.
    """
    params = {arg.spelling: arg for arg in cursor.get_arguments()}
    totals: dict[str, tuple[int, int]] = {}

    for name, arg in params.items():
        pointee = arg.type.get_pointee()
        if not pointee.spelling:
            continue
        r, w = _count_deref_rw(cursor, name, a_db)
        totals[name] = (r, w)

    # trace through called functions
    for child in cursor.walk_preorder():
        if child.kind != CursorKind.CALL_EXPR or not child.spelling:
            continue
        fn_key = (CursorKind.FUNCTION_DECL, child.spelling)
        if fn_key not in graph.symbols:
            continue
        callee_cursor = a_db.find_cursor(
            kind=CursorKind.FUNCTION_DECL, term=child.spelling, definition=True
        )
        if isinstance(callee_cursor, list):
            callee_cursor = max(callee_cursor, key=lambda c: c.extent.end.line - c.extent.start.line)
        if not callee_cursor:
            continue
        mapping = _find_param_mapping(child, params, callee_cursor)
        for callee_param, caller_param in mapping.items():
            if caller_param not in totals:
                continue
            r, w = _count_deref_rw(callee_cursor, callee_param, a_db)
            prev_r, prev_w = totals[caller_param]
            totals[caller_param] = (prev_r + r, prev_w + w)

    return totals


def _gen_harness_hints(
    cursor: Cursor,
    globals: list[Symbol] | None = None,
    rw_counts: dict[str, tuple[int, int]] | None = None,
) -> dict[str, str]:
    """Generate harness comment hints from function signature and globals.

    Provides enough detail for a human to wire up setup/call/reset:
    sizes, types, pointee info, read/write counts, and globals with sizes.
    """
    lines = []
    lines.append("/*")
    lines.append(f" * Target: {cursor.spelling}")
    ret_size = cursor.result_type.get_size()
    lines.append(f" * Return: {cursor.result_type.spelling} (size: {ret_size} bytes)" if ret_size > 0 else f" * Return: {cursor.result_type.spelling}")
    lines.append(" *")
    lines.append(" * Parameters:")

    total_min = 0
    for arg in cursor.get_arguments():
        size = arg.type.get_size()
        pointee = arg.type.get_pointee()
        is_const = arg.type.is_const_qualified() or (pointee.spelling and pointee.is_const_qualified())
        if pointee.spelling:
            pointee_size = pointee.get_size()
            quals: list[str] = []
            if is_const:
                quals.append("const")
            if rw_counts and arg.spelling in rw_counts:
                r, w = rw_counts[arg.spelling]
                quals.append(f"reads={r} writes={w}")
            qual = f" ({', '.join(quals)})" if quals else ""
            lines.append(f" *   {arg.type.spelling} {arg.spelling}{qual}")
            if pointee_size > 0:
                lines.append(f" *     -> {pointee.spelling} ({pointee_size} bytes)")
                total_min += pointee_size
            else:
                lines.append(f" *     -> {pointee.spelling} (opaque)")
        else:
            suffix = f" ({size} bytes)" if size > 0 else ""
            lines.append(f" *   {arg.type.spelling} {arg.spelling}{suffix}")
            if size > 0:
                total_min += size

    if globals:
        mutable = [s for s in globals if "extern" not in s.source]
        externs = [s for s in globals if "extern" in s.source]
        if mutable:
            lines.append(" *")
            lines.append(" * Mutable globals referenced in call graph:")
            for sym in mutable:
                if sym.size > 0:
                    lines.append(f" *   {sym.name} ({sym.size} bytes)")
                    total_min += sym.size
                else:
                    lines.append(f" *   {sym.name} (opaque)")
        if externs:
            lines.append(" *")
            lines.append(" * Extern globals (linked, not fuzzed):")
            for sym in externs:
                lines.append(f" *   {sym.name}")

    lines.append(" *")
    lines.append(f" * Total size (all params + globals): {total_min} bytes")
    lines.append(" */")

    call_args = ", ".join(arg.spelling for arg in cursor.get_arguments())
    call_line = f"/* {cursor.spelling}({call_args}); */"

    return {
        "declarations": "\n".join(lines),
        "call": call_line,
        "minlen": str(total_min) if total_min > 0 else "0",
        "buflen": str(total_min) if total_min > 0 else "0",
    }


def gen_sourcefile(
    a_db: AstDatabase,
    function: str,
    f_db: FunctionDatabase | None = None,
    _source_path: Path | None = None,
    source_dir: Path | None = None,
    stubs: str = "",
    blacklist: set[str] | None = None,
    rw_analysis: bool = False,
) -> None:
    # --- Phase: discover ---
    log.phase_start("discover")

    # find target function cursor
    result = a_db.find_cursor(
        kind=CursorKind.FUNCTION_DECL,
        term=function,
        definition=True,
    )
    if isinstance(result, list):
        log.event("func_multi_def", f"Found {len(result)} definitions for {function}, selecting longest body", function=function, count=len(result))
        target_cursor = max(result, key=lambda c: c.extent.end.line - c.extent.start.line)
    elif isinstance(result, Cursor):
        target_cursor = result
    else:
        log.error("target_not_found", f"Could not find function {function}", function=function)
        exit(1)

    log.phase_end("discover")

    # --- Phase: resolve ---
    log.phase_start("resolve")

    # resolve dependencies into a graph
    dep_rslvr = DependencyResolver(a_db, blacklist=blacklist)
    graph = DepGraph(blacklist=blacklist or set())
    # add the target function itself
    fn_source = a_db.get_content_from_ast_api(target_cursor)
    if not fn_source:
        log.error("func_source_fail", f"Could not extract source for target function {function}", function=function)
        exit(1)
    fn_sym = Symbol(
        name=function,
        kind=CursorKind.FUNCTION_DECL,
        source=fn_source,
    )
    graph.add(fn_sym)
    root_ctx = ResolveContext(chain=(function,))
    fn_sym.deps = dep_rslvr.resolve_deps_graph(target_cursor, graph, root_ctx)

    # resolve macros referenced in the graph (iterative, transitive)
    dep_rslvr.resolve_macros_graph(graph)

    # cross-TU resolution: resolve functions not defined in the primary TU
    if f_db and dep_rslvr.unresolved_functions:
        resolved_fns: set[str] = set()
        tu_cache: dict[Path, AstDatabase] = {a_db.source_path: a_db}
        tu_cache_lock = Lock()
        unresolved = dep_rslvr.unresolved_functions - (blacklist or set())

        def _resolve_cross_tu_fn(fn_name: str) -> set[str]:
            """Resolve a single cross-TU function. Returns new unresolved names."""
            result = f_db.find_function(fn_name, exact=True)
            if result is None:
                log.warning("cross_tu_miss", f"Cross-TU: {fn_name} not found in function database", function=fn_name)
                return set()
            fn_path = Path(result["file"])
            log.event("cross_tu_link", f"Cross-TU: resolving {fn_name} from {fn_path.name}", function=fn_name, file=fn_path.name)
            # reuse TU if we already created one for this file
            with tu_cache_lock:
                xtu_db = tu_cache.get(fn_path)
            if xtu_db is None:
                xtu_db = AstDatabase(a_db.compile_db, fn_path)
                with tu_cache_lock:
                    tu_cache.setdefault(fn_path, xtu_db)
                    xtu_db = tu_cache[fn_path]
            # find the function cursor in the new TU
            fn_cur = xtu_db.find_cursor(
                kind=CursorKind.FUNCTION_DECL, term=fn_name, definition=True
            )
            if isinstance(fn_cur, list):
                fn_cur = max(fn_cur, key=lambda c: c.extent.end.line - c.extent.start.line)
            if not fn_cur or isinstance(fn_cur, list):
                log.warning("cross_tu_cursor_fail", f"Cross-TU: could not find {fn_name} cursor", function=fn_name)
                return set()
            # add function and resolve its deps
            fn_source = xtu_db.get_content_from_ast_api(fn_cur)
            if not fn_source:
                log.warning("cross_tu_source_fail", f"Cross-TU: could not extract source for {fn_name}", function=fn_name)
                return set()
            fn_sym = Symbol(name=fn_name, kind=CursorKind.FUNCTION_DECL, source=fn_source)
            graph.add(fn_sym)
            xtu_rslvr = DependencyResolver(xtu_db, blacklist=blacklist)
            xtu_ctx = root_ctx.child(fn_name)
            fn_sym.deps = xtu_rslvr.resolve_deps_graph(fn_cur, graph, xtu_ctx)
            xtu_rslvr.resolve_macros_graph(graph)
            return xtu_rslvr.unresolved_functions

        while unresolved - resolved_fns:
            batch = unresolved - resolved_fns
            if not batch:
                break
            resolved_fns |= batch
            new_unresolved: set[str] = set()
            with ThreadPoolExecutor(max_workers=THREADS) as pool:
                futures = {pool.submit(_resolve_cross_tu_fn, fn): fn for fn in batch}
                for future in as_completed(futures):
                    new_unresolved |= future.result()
            unresolved |= new_unresolved - (blacklist or set())
        log.event("cross_tu_done", f"Cross-TU: resolved {len(resolved_fns)} functions from {len(tu_cache)} TUs", resolved=len(resolved_fns), tu_count=len(tu_cache))

        # post-pass: resolve incomplete typedefs using cross-TU databases
        # find typedefs whose underlying struct has an empty source (forward-decl only)
        for sym_key, sym in list(graph.symbols.items()):
            if sym.kind != CursorKind.TYPEDEF_DECL:
                continue
            # check if this typedef wraps a forward-declared struct
            from re import search as re_search
            match = re_search(r"\bstruct\s+(\w+)", sym.source)
            if not match:
                continue
            inner_name = match.group(1)
            inner_key = (CursorKind.STRUCT_DECL, inner_name)
            if not graph.has(*inner_key):
                continue
            inner_sym = graph.symbols[inner_key]
            if inner_sym.source.strip():
                continue  # already has a body
            # typedef's source already contains the body inline
            if re_search(rf"\bstruct\s+{inner_name}\s*\{{", sym.source):
                continue
            # try each cached TU to find the struct definition
            for xtu_db in tu_cache.values():
                cursor = xtu_db.find_cursor(
                    kind=CursorKind.STRUCT_DECL, term=inner_name, definition=True
                )
                if isinstance(cursor, list):
                    cursor = cursor[0]
                if cursor and cursor.is_definition():
                    source = xtu_db.get_content_from_ast_api(cursor)
                    if source and source.strip():
                        inner_sym.source = source
                        xtu_rslvr = DependencyResolver(xtu_db, blacklist=blacklist)
                        fwd_ctx = root_ctx.child(inner_name)
                        inner_sym.deps = xtu_rslvr.resolve_deps_graph(cursor, graph, fwd_ctx)
                        log.event("cross_tu_fwd_struct", f"Cross-TU: resolved forward-declared struct {inner_name} from {xtu_db.source_path.name}", symbol=inner_name, file=xtu_db.source_path.name, chain=fwd_ctx.chain)
                        break

    # resolve sizeof(TypeName) dependencies for correct toposort ordering
    dep_rslvr.resolve_sizeof_graph(graph)

    # graph summary
    kind_counts: dict[str, int] = {}
    for sym in graph.symbols.values():
        k = str(sym.kind).rsplit(".", 1)[-1]
        kind_counts[k] = kind_counts.get(k, 0) + 1
    log.event("graph_summary", f"Dependency graph: {len(graph.symbols)} symbols ({', '.join(f'{v} {k}' for k, v in sorted(kind_counts.items()))})", total=len(graph.symbols), by_kind=kind_counts)

    log.phase_end("resolve")

    # --- Phase: generate ---
    log.phase_start("generate")

    # find callers via coccinelle (if available)
    callers = ""
    if source_dir:
        callers = _find_callers(function, source_dir)
        if callers:
            caller_count = callers.count("() —")  # each caller line has "fn() — file:line"
            log.event("callers_found", f"Found {caller_count} callers via coccinelle", function=function, count=caller_count)

    # generate harness
    hints: dict[str, str] = {}
    def _is_static_const_initialized(src: str) -> bool:
        """Check if a VAR_DECL source is a static const with an initializer."""
        # Normalize: check the declaration prefix before any '='
        decl = src.split("=", 1)[0] if "=" in src else src
        return "static" in decl and "const" in decl and "=" in src

    resolved_globals = [
        sym for sym in graph.symbols.values()
        if sym.kind == CursorKind.VAR_DECL
        and not _is_static_const_initialized(sym.source)
    ]
    if target_cursor:
        rw_counts = _collect_rw_counts(target_cursor, graph, a_db) if rw_analysis else None
        hints = _gen_harness_hints(target_cursor, globals=resolved_globals, rw_counts=rw_counts)

    # render types in topological order
    types_rendered = graph.render()

    output = fuzz_template.substitute(
        includes="",
        stubs=stubs,
        types=types_rendered,
        callers=callers,
        declarations=hints.get("declarations", ""),
        setup=hints.get("setup", ""),
        call=hints.get("call", ""),
        reset=hints.get("reset", ""),
        minlen=hints.get("minlen", "0"),
        buflen=hints.get("buflen", "0"),
    )

    log.phase_end("generate")

    # --- Phase: output ---
    log.phase_start("output")

    # write output to fuzz.c, run clang-format if available
    output_path = "fuzz.c"
    with open(output_path, "w") as f:
        f.write(output)
    log.event("harness_written", f"Wrote {output_path} ({len(output)} bytes)", path=output_path, size=len(output))

    clang_fmt = which("clang-format")
    if clang_fmt:
        run([clang_fmt, "-i", output_path])
        log.event("clang_format", f"Formatted {output_path} with clang-format", path=output_path)

    log.phase_end("output")


def main() -> None:
    parser = ArgumentParser(
        description="Function Instrumentation tool for fuzzing",
    )
    parser.add_argument(
        "-f", "--function", help="Function to instrument", type=str, required=True
    )
    parser.add_argument(
        "-c", "--compile-db", help="Path to compile_commands.json", type=Path, required=True
    )
    parser.add_argument("-s", "--stubs", help="Path to stub file", type=Path)
    parser.add_argument("-v", "--verbose", help="Verbose output", action="store_true")
    parser.add_argument("--rw-analysis", help="Analyze pointer parameter read/write counts (slow)", action="store_true")
    parser.add_argument("--log-file", help="JSON log file path (default: excido.log.jsonl)", default="excido.log.jsonl")
    args = parser.parse_args()  # type: ignore

    function: str = args.function  # type: ignore
    compile_db_path: Path = args.compile_db  # type: ignore
    stubs_path: Path | None = args.stubs  # type: ignore
    verbose: bool = args.verbose  # type: ignore
    log_file: str = args.log_file  # type: ignore
    function_search_result: dict | None
    function_path: Path

    # initialize structured logging
    log.setup(verbose=verbose, log_file=log_file)

    # sanity check
    if not compile_db_path.exists():
        log.error("db_missing", f"Compile database {compile_db_path} does not exist", path=str(compile_db_path))
        exit(1)

    # create FunctionDatabase
    f_db = FunctionDatabase(
        db_path=CACHE_PATH / "functions.json",
        compile_db=compile_db_path,
        verbose=verbose,
    )

    # find function by iterating keys
    function_search_result = f_db.find_function(function)
    if function_search_result is None:
        exit(1)
    function_path = Path(function_search_result["file"])
    log.event("func_located", f"Found function {function} in {function_path}", function=function, file=str(function_path))

    # create CompileDB and AstDatabase
    log.phase_start("parse")
    compile_db = CompileDB(compile_db_path)
    a_db = AstDatabase(compile_db, function_path)
    log.phase_end("parse")

    # read stubs and derive blacklist
    stubs = ""
    blacklist: set[str] = set()
    if stubs_path:
        if not stubs_path.exists():
            log.error("stubs_missing", f"Stub file {stubs_path} does not exist", path=str(stubs_path))
            exit(1)
        stubs = stubs_path.read_text()
        blacklist = _parse_stub_blacklist(stubs_path)

    # create fuzz.c
    # scope coccinelle search to the target's directory (fast, but may miss callers in other dirs)
    gen_sourcefile(a_db, function, f_db=f_db, _source_path=function_path, source_dir=function_path.parent, stubs=stubs, blacklist=blacklist, rw_analysis=args.rw_analysis)


if __name__ == "__main__":
    main()
