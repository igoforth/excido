from os import chdir, getcwd
from pathlib import Path
from subprocess import check_output
from sys import exit
from threading import Lock

from excido.clang import (
    CompilationDatabase,
    CompileCommand,
    CompileCommands,
    TranslationUnit,
)
from excido.constants import (
    CACHE_PATH,
)
from excido import log

_cwd_lock = Lock()


def _clean_args_for_libclang(args: list[str]) -> list[str]:
    """Strip flags that system clang / libclang doesn't understand.

    Removes: compiler binary (args[0]), -o <file>, -c,
    --compile-and-analyze <path>, -Xanalyzer <arg> pairs,
    and the source file (last arg).
    """
    drop_flags = {
        "-c", "--analyzer-Werror",
        "-fenable-pt-opts", "-fpartition-cold",
        "-Wno-undefined-optimized", "-Werror",
    }
    drop_flag_with_arg = {
        "-o", "--compile-and-analyze", "-Xanalyzer",
        "-fprofile-sample-use", "-target",
    }
    cleaned = []
    skip_next = False
    for _i, arg in enumerate(args[1:-1], start=1):
        if skip_next:
            skip_next = False
            continue
        if any(arg.startswith(f + "=") for f in drop_flag_with_arg):
            continue
        if arg in drop_flag_with_arg:
            skip_next = True
            continue
        if arg in drop_flags:
            continue
        if arg.startswith("-G") and (len(arg) == 2 or arg[2:].isdigit()):
            if len(arg) == 2:
                skip_next = True
            continue
        if arg.startswith("-mv") and arg[3:].isdigit():
            continue
        cleaned.append(arg)
    return cleaned


class CompileDB:
    def __init__(self, cc_path: Path) -> None:
        self.cc_path: Path = cc_path.resolve()
        self._ast_cache: dict[Path, Path] = {}
        self._tu_cache: dict[Path, TranslationUnit] = {}

    @property
    def db(self) -> CompilationDatabase:
        if not hasattr(self, "_db"):
            # fromDirectory expects a directory containing compile_commands.json
            self._db = self._load_database(self.cc_path.parent if self.cc_path.is_file() else self.cc_path)
        if not self._db:
            log.error("db_unavailable", f"Failed to load compilation database from {self.cc_path}", path=str(self.cc_path))
            exit(1)
        return self._db

    def get_compile_command(self, source_path: Path) -> CompileCommand | None:
        comp_cmds: CompileCommands = self.db.getCompileCommands(str(source_path))
        if not comp_cmds:
            log.error("cc_not_found", f"Failed to find {source_path} in compile_commands.json", path=str(source_path))
            return None
        if len(comp_cmds) > 1:
            log.warning("cc_multiple", f"Found multiple compile commands for {source_path}, using first", path=str(source_path), count=len(comp_cmds))
        return comp_cmds[0]

    def create_ast(
        self, source_path: Path, verbose: bool = False
    ) -> Path | None:
        """Create a JSON AST dump for the given source file. Caches by resolved source path."""
        resolved = source_path.resolve()
        cached = self._ast_cache.get(resolved)
        if cached:
            return cached
        comp_cmd = self.get_compile_command(source_path)
        if not comp_cmd:
            return None
        ast_path = self._create_ast(comp_cmd)
        self._ast_cache[resolved] = ast_path
        return ast_path

    def create_translation_unit(
        self, source_path: Path, verbose: bool = False
    ) -> TranslationUnit | None:
        """Create a TranslationUnit for the given source file. Caches by resolved source path."""
        resolved = source_path.resolve()
        cached = self._tu_cache.get(resolved)
        if cached:
            return cached
        comp_cmd = self.get_compile_command(source_path)
        if not comp_cmd:
            return None
        tu = self._create_translation_unit(comp_cmd)
        if tu:
            self._tu_cache[resolved] = tu
        return tu

    @staticmethod
    def _load_database(db_dir: Path) -> CompilationDatabase | None:
        log.event("db_load", f"Loading compile_commands.json from {db_dir}", path=str(db_dir))
        cdb: CompilationDatabase | None = CompilationDatabase.fromDirectory(
            str(db_dir)
        )
        if not cdb:
            log.error("db_load_fail", f"Failed to load compile_commands.json from {db_dir}", path=str(db_dir))
            return None
        return cdb

    @staticmethod
    def _create_ast(comp_cmd: CompileCommand) -> Path:
        """Create a JSON AST dump using system clang.

        Returns the path to the written ast.json file.
        """
        args: list[str] = [str(a) for a in comp_cmd.arguments]

        work_dir = Path(comp_cmd.directory).resolve()
        source_file = (work_dir / args[-1]).resolve()

        source_stem = source_file.stem
        ast_path = (CACHE_PATH / f"{source_stem}.ast.json").resolve()
        log.event("ast_create", f"Creating JSON AST dump for {source_file.name}", file=str(source_file))
        ast_args = ["clang"] + _clean_args_for_libclang(args) + [
            "-Xclang", "-ast-dump=json",
            "-fsyntax-only",
            "-fno-color-diagnostics",
            "-Wno-visibility",
            str(source_file),
        ]
        log.debug("ast_cmd", f"Running: {ast_args}", cmd=ast_args, args=args)
        ast_str = check_output(ast_args, encoding="utf-8", errors="ignore", cwd=work_dir)
        ast_path.open("w").write(ast_str)
        return ast_path

    @staticmethod
    def _create_translation_unit(comp_cmd: CompileCommand) -> TranslationUnit | None:
        args: list[str] = [str(a) for a in comp_cmd.arguments]

        work_dir = Path(comp_cmd.directory).resolve()
        source_file = str((work_dir / args[-1]).resolve())
        log.event("tu_create", f"Creating TranslationUnit for {Path(source_file).name}", file=source_file)

        tu_args = _clean_args_for_libclang(args)
        log.debug("tu_cmd", f"from_source args: {tu_args}", file=source_file, args=tu_args)
        with _cwd_lock:
            saved = getcwd()
            chdir(work_dir)
            try:
                tu: TranslationUnit = TranslationUnit.from_source(
                    source_file,
                    args=tu_args,
                    options=TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
                )
            finally:
                chdir(saved)
        return tu
