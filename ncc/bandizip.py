"""반디집 CLI(bz.exe) 탐색 및 실행 래퍼.

공식 문서(https://www.bandisoft.com/bandizip/help/parameter/) 기준 사용 명령
  bz.exe c  -fmt:zip -l:0 -y -r- -consolemode:utf8 <archive> <files...>   새 압축파일 생성
  bz.exe a  ...                                                          파일 추가 (명령줄 길이 분할용)
  bz.exe t  -consolemode:utf8 <archive>                                  무결성 검사
-l:0 = 저장(무압축), -l:1 = 빠른 압축, -l:5 = 표준
"""
from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Sequence

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000

# Windows CreateProcess 명령줄 한도는 32767자. 여유를 두고 분할한다.
MAX_CMDLINE_CHARS = 24000


def find_bandizip() -> str | None:
    """설치된 bz.exe 경로를 찾는다."""
    cands: list[str] = []
    for env in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        base = os.environ.get(env)
        if base:
            cands.append(os.path.join(base, "Bandizip", "bz.exe"))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cands.append(os.path.join(local, "Programs", "Bandizip", "bz.exe"))
        cands.append(os.path.join(local, "Bandizip", "bz.exe"))
    if IS_WINDOWS:
        cands.extend(_registry_candidates())
    w = shutil.which("bz.exe") or shutil.which("bz")
    if w:
        cands.append(w)
    seen = set()
    for c in cands:
        key = os.path.normcase(os.path.abspath(c))
        if key in seen:
            continue
        seen.add(key)
        if os.path.isfile(c):
            return c
    return None


def _registry_candidates() -> list[str]:
    out: list[str] = []
    try:
        import winreg
    except ImportError:
        return out
    keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Bandizip.exe"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Bandizip.exe"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Bandizip"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Bandizip"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Bandizip"),
    ]
    for hive, sub in keys:
        try:
            with winreg.OpenKey(hive, sub) as k:
                for val in ("", "Path", "InstallLocation", "DisplayIcon", "UninstallString"):
                    try:
                        v, _ = winreg.QueryValueEx(k, val)
                    except OSError:
                        continue
                    if not isinstance(v, str) or not v:
                        continue
                    v = v.strip().strip('"').split('",')[0].split(",")[0]
                    d = v if os.path.isdir(v) else os.path.dirname(v)
                    out.append(os.path.join(d, "bz.exe"))
        except OSError:
            continue
    return out


def is_valid_bz(path: str | None) -> bool:
    return bool(path) and os.path.isfile(path) and os.path.basename(path).lower() in ("bz.exe", "bz")


def decode_output(b: bytes) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("cp949", errors="replace")


@dataclass
class RunResult:
    returncode: int
    output: str
    cancelled: bool = False


class BandizipRunner:
    """bz.exe 실행기. command_prefix는 테스트용(가짜 bz) 교체를 위해 리스트로 받는다."""

    def __init__(self, bz_path: str | Sequence[str]):
        if isinstance(bz_path, (list, tuple)):
            self.prefix = list(bz_path)
        else:
            self.prefix = [bz_path]

    # ------------------------------------------------------------------
    def run(
        self,
        args: list[str],
        cwd: str | None,
        cancel_event: threading.Event | None = None,
        on_line: Callable[[str], None] | None = None,
        timeout: float | None = None,
    ) -> RunResult:
        cmd = self.prefix + args
        kwargs = dict(
            cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, shell=False,
        )
        if IS_WINDOWS:
            kwargs["creationflags"] = CREATE_NO_WINDOW
        proc = subprocess.Popen(cmd, **kwargs)
        q: queue.Queue = queue.Queue()

        def reader():
            buf = b""
            try:
                while True:
                    chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        idx = -1
                        for sep in (b"\n", b"\r"):
                            i = buf.find(sep)
                            if i != -1 and (idx == -1 or i < idx):
                                idx = i
                        if idx == -1:
                            break
                        line, buf = buf[:idx], buf[idx + 1:]
                        q.put(line)
                if buf:
                    q.put(buf)
            finally:
                q.put(None)

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        lines: list[str] = []
        cancelled = False
        start = time.monotonic()
        done_reading = False
        while True:
            try:
                item = q.get(timeout=0.2)
                if item is None:
                    done_reading = True
                else:
                    s = decode_output(item).strip()
                    if s:
                        lines.append(s)
                        if on_line:
                            on_line(s)
            except queue.Empty:
                pass
            if cancel_event is not None and cancel_event.is_set() and proc.poll() is None:
                cancelled = True
                self._kill(proc)
            if timeout and time.monotonic() - start > timeout and proc.poll() is None:
                lines.append("시간 초과로 반디집 프로세스를 종료했습니다")
                self._kill(proc)
            if done_reading and proc.poll() is not None:
                break
            if done_reading:
                try:
                    proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
        rc = proc.wait()
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
        return RunResult(rc, "\n".join(lines), cancelled)

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            proc.kill()
        except OSError:
            pass

    # ------------------------------------------------------------------
    @staticmethod
    def file_args(names: list[str]) -> list[str]:
        """현재 폴더 기준 상대 경로 인수. '-'로 시작하는 이름이 스위치로 해석되지 않도록 .\\ 접두."""
        sep = "\\" if IS_WINDOWS else "/"
        return ["." + sep + n for n in names]

    @staticmethod
    def chunk_files(file_args: list[str], fixed_len: int) -> list[list[str]]:
        chunks: list[list[str]] = []
        cur: list[str] = []
        cur_len = fixed_len
        for a in file_args:
            add = len(a) + 3  # 따옴표/공백
            if cur and cur_len + add > MAX_CMDLINE_CHARS:
                chunks.append(cur)
                cur, cur_len = [], fixed_len
            cur.append(a)
            cur_len += add
        if cur:
            chunks.append(cur)
        return chunks

    def create_zip(
        self, archive: str, names: list[str], cwd: str, level: int,
        cancel_event=None, on_line=None,
    ) -> RunResult:
        switches = ["-fmt:zip", f"-l:{int(level)}", "-y", "-r-", "-consolemode:utf8"]
        fa = self.file_args(names)
        fixed = sum(len(x) + 3 for x in self.prefix + switches) + len(archive) + 8
        chunks = self.chunk_files(fa, fixed)
        outputs = []
        for i, ch in enumerate(chunks):
            cmd = "c" if i == 0 else "a"
            r = self.run([cmd, *switches, archive, *ch], cwd=cwd, cancel_event=cancel_event, on_line=on_line)
            outputs.append(r.output)
            if r.returncode != 0 or r.cancelled:
                return RunResult(r.returncode, "\n".join(outputs), r.cancelled)
        return RunResult(0, "\n".join(outputs))

    def test(self, archive: str, cancel_event=None, on_line=None) -> RunResult:
        return self.run(["t", "-consolemode:utf8", archive], cwd=os.path.dirname(archive) or None,
                        cancel_event=cancel_event, on_line=on_line)


def python_runner_for_fake(fake_script: str) -> BandizipRunner:
    """테스트용: 가짜 bz 스크립트를 현재 파이썬으로 실행."""
    return BandizipRunner([sys.executable, fake_script])
