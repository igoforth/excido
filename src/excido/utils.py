from pathlib import Path
from threading import Lock

from excido import log


def confirm_item(item: str) -> bool:
    # prompt user to confirm item via stderr to avoid polluting log output
    import sys
    sys.stderr.write(f"Is this correct? {item}\n")
    confirm = input("Y/N: ")
    return confirm.lower() == "y"


_file_cache: dict[Path, str] = {}
_file_cache_bytes: dict[Path, bytes] = {}
_file_cache_bytes_lock = Lock()


def read_file_bytes(path: Path) -> bytes | Exception:
    with _file_cache_bytes_lock:
        cached = _file_cache_bytes.get(path)
        if cached is not None:
            return cached
    try:
        with open(path, "rb") as file:
            content = file.read()
    except FileNotFoundError as e:
        log.error("file_not_found", f"File not found: {path}", path=str(path))
        return e
    except IOError as e:
        log.error("read_io_error", f"IOError reading {path}: {e}", path=str(path), error=str(e))
        return e
    with _file_cache_bytes_lock:
        _file_cache_bytes.setdefault(path, content)
        return _file_cache_bytes[path]

def write_file(path: Path, content: str | bytes) -> None | Exception:
    try:
        if not path.parent.exists():
            log.debug("dir_create", f"Parent(s) not found, creating: {path}", path=str(path))
            path.parent.mkdir(parents=True)
            path.touch()
        elif not path.exists():
            log.debug("file_create", f"File not found, creating: {path}", path=str(path))
            path.touch()
        if isinstance(content, str):
            log.debug("file_write", f"Writing string to file: {path}", path=str(path), mode="text")
            with path.open("w") as f:
                f.write(content)
        else:
            log.debug("file_write", f"Writing bytes to file: {path}", path=str(path), mode="bytes")
            with path.open("wb") as f:
                f.write(content)
    except IOError as e:
        log.error("write_io_error", f"IOError writing {path}: {e}", path=str(path), error=str(e))
        return e


def _find(buf: memoryview, pos: int, delim: bytes) -> int:
    # Loop through the buffer starting from pos
    for i in range(pos, len(buf)):
        # Check if the current slice matches the delimiter
        if buf[i : i + len(delim)] == delim:
            return i
    return -1  # Return -1 if not found


# input buf, grab to delim, output (line, rest)
def grab(
    buf: memoryview,
    pos: int,
    delim: bytes,
) -> tuple[memoryview | None, int]:
    i = _find(buf, pos, delim)
    if i == -1:
        return None, pos
    return buf[pos:i], i
