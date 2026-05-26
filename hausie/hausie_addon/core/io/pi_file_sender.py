from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
import platform
import shutil
import subprocess
from typing import Iterable


def _escape_remote_path(path: str) -> str:
    return path.replace("\\", "/").replace(" ", "\\ ")


@dataclass
class PiFileSender:
    host: str
    user: str
    port: int = 22
    key_path: str | None = None
    use_scp_legacy: bool = False
    prefer_rsync: bool = True
    replace_existing: bool = True
    path_map: dict[str, str] = field(default_factory=dict)
    timeout_s: int = 20
    ssh_options: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.key_path:
            self.key_path = self._normalize_key_path(self.key_path)
        if not self.ssh_options:
            if self.key_path:
                self.ssh_options = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
            else:
                self.ssh_options = ["-o", "ConnectTimeout=8"]

    def _normalize_key_path(self, path: str) -> str:
        if not path:
            return path
        if os.name == "posix":
            rel = platform.release().lower()
            if "microsoft" in rel and ":" in path:
                drive, rest = path.split(":", 1)
                rest = rest.lstrip("\\/").replace("\\", "/")
                return f"/mnt/{drive.lower()}/{rest}"
        return path

    def _ssh_base(self) -> list[str]:
        cmd = ["ssh", "-p", str(self.port)]
        if self.key_path:
            cmd.extend(["-i", self.key_path])
        if self.ssh_options:
            cmd.extend(self.ssh_options)
        cmd.append(f"{self.user}@{self.host}")
        return cmd

    def _scp_base(self) -> list[str]:
        cmd = ["scp", "-P", str(self.port)]
        if self.use_scp_legacy:
            cmd.append("-O")
        if self.key_path:
            cmd.extend(["-i", self.key_path])
        if self.ssh_options:
            cmd.extend(self.ssh_options)
        return cmd

    def _rsync_base(self) -> list[str]:
        cmd = ["rsync", "-a", "-z", "-e", f"ssh -p {self.port}"]
        if self.key_path:
            cmd[-1] += f" -i {self.key_path}"
        if self.ssh_options:
            cmd[-1] += " " + " ".join(self.ssh_options)
        return cmd

    def _ensure_tool(self, name: str) -> None:
        if not shutil.which(name):
            raise RuntimeError(f"Required tool not found in PATH: {name}")

    def _stdin(self):
        if "BatchMode=yes" in self.ssh_options:
            return subprocess.DEVNULL
        return None

    def _remote_target(self, remote_path: str) -> str:
        safe_path = _escape_remote_path(remote_path)
        return f"{self.user}@{self.host}:{safe_path}"

    def _run(self, args: list[str]) -> None:
        subprocess.run(args, check=True, stdin=self._stdin(), timeout=self.timeout_s)

    def read_remote_text(self, remote_path: str) -> str:
        self._ensure_tool("ssh")
        safe_path = _escape_remote_path(remote_path)
        cmd = self._ssh_base() + [f"cat {safe_path}"]
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            stdin=self._stdin(),
            timeout=self.timeout_s,
        )
        return result.stdout

    def _remote_exists(self, remote_path: str) -> bool:
        safe_path = _escape_remote_path(remote_path)
        cmd = self._ssh_base() + [f"test -e {safe_path}"]
        result = subprocess.run(cmd, stdin=self._stdin(), timeout=self.timeout_s)
        return result.returncode == 0

    def remove_remote(self, remote_path: str) -> None:
        safe_path = _escape_remote_path(remote_path)
        cmd = self._ssh_base() + [f"rm -rf {safe_path}"]
        self._run(cmd)

    def ensure_remote_dir(self, remote_dir: str) -> None:
        if not remote_dir:
            return
        safe_dir = _escape_remote_path(remote_dir)
        cmd = self._ssh_base() + [f"mkdir -p {safe_dir}"]
        self._run(cmd)

    def send_file(self, local_path: str | Path, remote_path: str) -> None:
        local_path = Path(local_path)
        if not local_path.exists() or not local_path.is_file():
            raise FileNotFoundError(f"Local file not found: {local_path}")

        if self.replace_existing and self._remote_exists(remote_path):
            self.remove_remote(remote_path)

        remote_dir = os.path.dirname(remote_path)
        if remote_dir:
            self.ensure_remote_dir(remote_dir)

        self._ensure_tool("scp")
        cmd = self._scp_base() + [str(local_path), self._remote_target(remote_path)]
        self._run(cmd)

    def send_dir(self, local_dir: str | Path, remote_dir: str) -> None:
        local_dir = Path(local_dir)
        if not local_dir.exists() or not local_dir.is_dir():
            raise FileNotFoundError(f"Local directory not found: {local_dir}")

        if self.replace_existing and self._remote_exists(remote_dir):
            self.remove_remote(remote_dir)

        if self.prefer_rsync and shutil.which("rsync"):
            self.ensure_remote_dir(remote_dir)
            src = str(local_dir)
            if not src.endswith(os.sep):
                src = f"{src}{os.sep}"
            cmd = self._rsync_base() + [src, self._remote_target(remote_dir)]
            self._run(cmd)
            return

        self._ensure_tool("scp")
        remote_parent = os.path.dirname(remote_dir.rstrip("/"))
        if remote_parent:
            self.ensure_remote_dir(remote_parent)
        cmd = self._scp_base() + ["-r", str(local_dir), self._remote_target(remote_parent or remote_dir)]
        self._run(cmd)

    def send(self, local_path: str | Path, remote_path: str) -> None:
        local_path = Path(local_path)
        if local_path.is_dir():
            self.send_dir(local_path, remote_path)
        else:
            self.send_file(local_path, remote_path)

    def send_relative(self, local_path: str | Path, local_root: str | Path, remote_root: str) -> str:
        local_path = Path(local_path).resolve()
        local_root = Path(local_root).resolve()
        rel = local_path.relative_to(local_root)
        remote_path = f"{remote_root.rstrip('/')}/{rel.as_posix()}"
        self.send(local_path, remote_path)
        return remote_path

    def send_mapped(self, local_path: str | Path) -> str:
        local_path = Path(local_path).resolve()
        if not self.path_map:
            raise ValueError("path_map is empty; configure local->remote roots first.")
        best_root = None
        for root in self.path_map.keys():
            root_path = Path(root).resolve()
            if str(local_path).startswith(str(root_path)):
                if best_root is None or len(str(root_path)) > len(str(Path(best_root).resolve())):
                    best_root = root
        if best_root is None:
            raise ValueError(f"No mapping found for {local_path}")
        remote_root = self.path_map[best_root]
        return self.send_relative(local_path, best_root, remote_root)

    def send_many(self, items: Iterable[tuple[str | Path, str]]) -> None:
        for local_path, remote_path in items:
            self.send(local_path, remote_path)
