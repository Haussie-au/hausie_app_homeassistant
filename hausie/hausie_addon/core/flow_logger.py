from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import os
import time
from pathlib import Path

_LOG_FILE = os.getenv("HAUSIE_LOG_FILE", "").strip()
_LOG_MAX = int(os.getenv("HAUSIE_LOG_MAX_BYTES", "1048576"))
_CLEAR_ON_START = os.getenv("TEST_LOG_CLEAR_ON_START", "").strip().lower() in {"1", "true", "yes"}
_LOG_STDOUT = os.getenv("HAUSIE_LOG_TO_STDOUT", "true").strip().lower() not in {"0", "false", "no"}


def _maybe_clear_log() -> None:
    if not _CLEAR_ON_START or not _LOG_FILE:
        return
    try:
        path = Path(_LOG_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    except Exception:
        pass


def _append_log(line: str) -> None:
    if not _LOG_FILE:
        return
    try:
        path = Path(_LOG_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if _LOG_MAX > 0 and path.stat().st_size > _LOG_MAX:
            # Keep last half of the file to avoid unbounded growth.
            data = path.read_text(encoding="utf-8", errors="ignore")
            lines = data.splitlines()
            tail = "\n".join(lines[-max(200, len(lines) // 2):])
            path.write_text(tail + "\n", encoding="utf-8")
    except Exception:
        pass


@dataclass(frozen=True)
class FlowLogger:
    category: str

    def _log(self, status: str, message: str) -> None:
        status_value = status or "info"
        category_value = self.category or "core"
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{ts} [{category_value}][{status_value}] {message}"
        if _LOG_STDOUT:
            print(line, flush=True)
        _append_log(line)

    def info(self, message: str) -> None:
        self._log("info", message)

    def start(self, message: str) -> None:
        self._log("start", message)

    def ok(self, message: str) -> None:
        self._log("ok", message)

    def warn(self, message: str) -> None:
        self._log("warn", message)

    def error(self, message: str) -> None:
        self._log("error", message)

    def skip(self, message: str) -> None:
        self._log("skip", message)

    def script_start(self, name: str) -> None:
        label = name or "script"
        self._log("start", f"==== Script start: {label} ====")

    def script_end(self, name: str, ok: bool = True, elapsed_s: float | None = None) -> None:
        label = name or "script"
        suffix = ""
        if elapsed_s is not None:
            suffix = f" ({elapsed_s:.1f}s)"
        status = "ok" if ok else "error"
        self._log(status, f"==== Script end: {label}{suffix} ====")

    @contextmanager
    def script(self, name: str):
        start = time.time()
        self.script_start(name)
        ok = True
        try:
            yield
        except Exception as exc:
            ok = False
            self._log("error", f"Script failed: {name} ({exc})")
            raise
        finally:
            self.script_end(name, ok=ok, elapsed_s=time.time() - start)


def get_logger(category: str) -> FlowLogger:
    return FlowLogger(category=category)


_maybe_clear_log()
