from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass

_start = time.monotonic()
_phase = "init"


@dataclass(frozen=True)
class ResolveContext:
    """Immutable context threaded through recursive dependency resolution.

    Each recursive call creates a new child context via .child(), building
    up the resolution chain. The chain can be logged to understand why
    a particular type was pulled into the dependency graph.
    """

    chain: tuple[str, ...] = ()

    def child(self, name: str) -> ResolveContext:
        return ResolveContext(chain=self.chain + (name,))

    @property
    def depth(self) -> int:
        return len(self.chain)

    @property
    def breadcrumb(self) -> str:
        return " -> ".join(self.chain)


_main_thread_name = threading.current_thread().name


class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        thread = threading.current_thread().name
        data: dict = {
            "t": round(time.monotonic() - _start, 3),
            "level": record.levelname.lower(),
            "phase": getattr(record, "phase", _phase),
            "event": getattr(record, "event", "msg"),
            "msg": record.getMessage(),
        }
        if thread != _main_thread_name:
            data["thread"] = thread
        for k, v in getattr(record, "fields", {}).items():
            data[k] = v
        return json.dumps(data, default=str)


class HumanFormatter(logging.Formatter):
    _LEVEL_COLORS = {
        "DEBUG": "\033[90m",     # grey
        "INFO": "\033[36m",      # cyan
        "WARNING": "\033[33m",   # yellow
        "ERROR": "\033[31m",     # red
    }
    _RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        elapsed = time.monotonic() - _start
        phase = getattr(record, "phase", _phase)
        event = getattr(record, "event", "")
        fields = getattr(record, "fields", {})
        thread = threading.current_thread().name

        tag = f" | {event}" if event != "msg" else ""
        color = self._LEVEL_COLORS.get(record.levelname, "")
        reset = self._RESET if color else ""

        thread_str = ""
        if thread != _main_thread_name:
            thread_str = f" ({thread})"

        chain = fields.get("chain")
        chain_str = ""
        if chain and len(chain) > 1:
            chain_str = f" [{' -> '.join(chain)}]"

        return (
            f"{color}[{elapsed:06.1f}] {record.levelname:<7} "
            f"{phase:<10}{tag} | "
            f"{record.getMessage()}{chain_str}{thread_str}{reset}"
        )


def setup(
    verbose: bool = False,
    log_file: str = "excido.log.jsonl",
) -> None:
    root = logging.getLogger("excido")
    root.handlers = []
    root.propagate = False
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    # human-readable on stderr
    stderr_handler = logging.StreamHandler()
    stderr_handler.setFormatter(HumanFormatter())
    stderr_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(stderr_handler)

    # JSON to file (always captures everything including DEBUG)
    file_handler = logging.FileHandler(log_file, mode="w")
    file_handler.setFormatter(StructuredFormatter())
    file_handler.setLevel(logging.DEBUG)
    root.addHandler(file_handler)


def event(
    name: str,
    _msg: str | None = None,
    _level: int = logging.INFO,
    **fields: object,
) -> None:
    logger = logging.getLogger("excido")
    record = logger.makeRecord(
        "excido", _level, "", 0,
        _msg or name, (), None,
    )
    record.event = name  # type: ignore[attr-defined]
    record.phase = _phase  # type: ignore[attr-defined]
    record.fields = fields  # type: ignore[attr-defined]
    logger.handle(record)


def debug(name: str, _msg: str | None = None, **fields: object) -> None:
    event(name, _msg, logging.DEBUG, **fields)


def warning(name: str, _msg: str | None = None, **fields: object) -> None:
    event(name, _msg, logging.WARNING, **fields)


def error(name: str, _msg: str | None = None, **fields: object) -> None:
    event(name, _msg, logging.ERROR, **fields)


def phase_start(name: str) -> None:
    global _phase
    _phase = name
    event("phase_start", f"Starting {name}")


def phase_end(name: str) -> None:
    event("phase_end", f"Finished {name}")
