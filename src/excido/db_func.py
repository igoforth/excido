
from json import loads
from pathlib import Path
from shutil import which
from subprocess import PIPE, CalledProcessError, run
from sys import exit

from excido.constants import THREADS
from excido import log

# Locate func_scanner binary: check PATH first, then build/ relative to repo root.
_FUNC_SCANNER: str | None = which("func_scanner")
if not _FUNC_SCANNER:
    _candidate = Path(__file__).resolve().parents[2] / "build" / "func_scanner"
    if _candidate.is_file():
        _FUNC_SCANNER = str(_candidate)

# func_scanner JSON format per entry:
#   {"file": str, "start_line": int, "start_col": int, "end_line": int, "end_col": int}
FuncEntry = dict[str, str | int]


class FunctionDatabase:
    def __init__(
        self,
        db_path: Path,
        compile_db: Path,
        verbose: bool = False,
    ) -> None:
        self._db_path: Path = db_path.resolve()
        self._compile_db: Path = compile_db.resolve()
        self._verbose: bool = verbose

    @property
    def verbose(self) -> bool:
        return self._verbose

    @property
    def db_path(self) -> Path:
        if not self._db_path.exists():
            self._scan()
        return self._db_path

    @property
    def db(self) -> dict[str, list[FuncEntry]]:
        if not hasattr(self, "_db"):
            if not self._db_path.exists():
                self._scan()
            else:
                self._db = loads(self._db_path.read_text())
        return self._db

    def _scan(self) -> None:
        """Run func_scanner on the compile database, writing JSON to db_path."""
        if not _FUNC_SCANNER:
            log.error(
                "scanner_not_found",
                "func_scanner binary not found. "
                "Build it with: cmake --build build --target func_scanner",
            )
            exit(1)

        cmd = [
            _FUNC_SCANNER,
            "-p", str(self._compile_db),
            "-o", str(self._db_path),
            "-j", str(THREADS),
        ]

        # TODO: consider this later
        # if self._verbose:
        #     cmd.append("-v")

        log.event("scanner_run", f"Running: {' '.join(cmd)}", cmd=cmd)

        try:
            result = run(cmd, stderr=PIPE)
            if self._verbose and result.stderr:
                for line in result.stderr.decode(errors="replace").splitlines():
                    log.debug("scanner_stderr", f"func_scanner: {line}")
            result.check_returncode()
        except CalledProcessError as e:
            stderr = e.stderr.decode(errors="replace") if e.stderr else ""
            log.error("scanner_fail", f"func_scanner failed (exit {e.returncode}): {stderr}", exit_code=e.returncode)
            exit(1)
        except FileNotFoundError:
            log.error("scanner_missing", f"func_scanner not found at {_FUNC_SCANNER}", path=_FUNC_SCANNER)
            exit(1)

        self._db = loads(self._db_path.read_text())
        log.event("scanner_done", f"Found {len(self._db)} functions", count=len(self._db))

    @staticmethod
    def _best_occurrence(entries: list[FuncEntry]) -> FuncEntry:
        """Pick the occurrence with the largest line span (most likely the definition)."""
        return max(entries, key=lambda e: e["end_line"] - e["start_line"])

    def find_function(
        self,
        function: str,
        exact: bool = False,
    ) -> FuncEntry | None:
        if exact:
            if function in self.db:
                return self._best_occurrence(self.db[function])
        else:
            for key in self.db:
                if function in key:
                    return self._best_occurrence(self.db[key])
        log.warning("func_not_found", f"Function {function} not found in database", function=function)
        return None
