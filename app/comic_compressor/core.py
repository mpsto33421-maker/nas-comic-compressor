from __future__ import annotations

import csv
from contextlib import contextmanager
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

IMAGES = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif', '.tif', '.tiff', '.avif'}
ARCHIVES = {'.zip', '.cbz', '.rar', '.cbr', '.7z', '.tar'}
ACCESS_RETRY_DELAYS = (0.2, 0.5, 1.0, 2.0)


class Cancelled(Exception):
    pass


class Control:
    """Pause at safe checkpoints; a running bz command finishes before pausing."""
    def __init__(self):
        self.cancelled = threading.Event()
        self.paused = threading.Event()

    def stop(self):
        self.cancelled.set()

    def check(self, allow_pause=True):
        while allow_pause and self.paused.is_set():
            if self.cancelled.wait(0.1):
                raise Cancelled('사용자가 중단했습니다')
        if self.cancelled.is_set():
            raise Cancelled('사용자가 중단했습니다')


@dataclass(frozen=True)
class ImageFile:
    name: str
    size: int
    mtime_ns: int


@dataclass
class Candidate:
    path: Path
    files: list[ImageFile]
    output: Path

    @property
    def size(self):
        return sum(f.size for f in self.files)


@dataclass
class Record:
    path: str
    status: str
    detail: str
    count: int = 0
    source_bytes: int = 0
    output: str = ''
    output_bytes: int = 0
    original_action: str = '보존'


@dataclass
class ScanResult:
    candidates: list[Candidate] = field(default_factory=list)
    records: list[Record] = field(default_factory=list)
    inspected: int = 0
    cancelled: bool = False


def is_link(st):
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, 'st_file_attributes', 0) & 0x400)


def snapshot(path: Path, control: Control):
    """Metadata only. Reject links, nested directories, and unreadable metadata."""
    images, dirs, others = [], [], []
    with os.scandir(path) as entries:
        for entry in entries:
            control.check()
            st = entry.stat(follow_symlinks=False)
            if is_link(st):
                raise OSError(f'링크/재분석 지점은 지원하지 않습니다: {entry.name}')
            if stat.S_ISDIR(st.st_mode):
                dirs.append(Path(entry.path))
            elif stat.S_ISREG(st.st_mode):
                if Path(entry.name).suffix.lower() in IMAGES:
                    images.append(ImageFile(entry.name, st.st_size, st.st_mtime_ns))
                else:
                    others.append(entry.name)
            else:
                raise OSError(f'일반 파일이 아닙니다: {entry.name}')
    images.sort(key=lambda f: (f.name.casefold(), f.name))
    return images, dirs, others


def existing_archive(output: Path):
    # scandir rather than exists: inaccessible parent must fail closed.
    wanted = {(output.stem + ext).casefold() for ext in ARCHIVES}
    with os.scandir(output.parent) as entries:
        return next((e.name for e in entries if e.name.casefold() in wanted), None)


def scan(root: Path, control: Control | None = None,
         progress: Callable = lambda *_: None) -> ScanResult:
    control = control or Control()
    result = ScanResult()
    pending = [Path(os.path.abspath(root))]
    sibling_names = {}
    try:
        while pending:
            control.check()
            path = pending.pop()
            result.inspected += 1
            try:
                if is_link(path.lstat()):
                    raise OSError('링크/재분석 지점은 탐색하지 않습니다')
                images, dirs, others = snapshot(path, control)
                pending.extend(sorted(dirs, reverse=True))
                if dirs:
                    reason = '하위 폴더가 있어 상위 폴더 보존 (직접 이미지도 보존)'
                elif not images:
                    reason = '이미지 없음 (기존 압축파일 및 일반 파일 보존)'
                else:
                    output = path.with_name(path.name + '.zip')
                    if output.parent not in sibling_names:
                        with os.scandir(output.parent) as entries:
                            sibling_names[output.parent] = {e.name.casefold() for e in entries}
                    conflict = next((output.stem + ext for ext in ARCHIVES
                                     if (output.stem + ext).casefold() in sibling_names[output.parent]), None)
                    if conflict:
                        reason = f'동일 이름 압축파일 존재: {conflict}'
                    else:
                        candidate = Candidate(path, images, output)
                        result.candidates.append(candidate)
                        rec = Record(str(path), '대상', '직접 이미지 파일만 압축; 다른 파일 보존',
                                     len(images), candidate.size, str(output))
                        result.records.append(rec)
                        progress(result.inspected, rec)
                        continue
                rec = Record(str(path), '건너뜀', reason)
            except OSError as exc:
                rec = Record(str(path), '스캔 오류', str(exc))
            result.records.append(rec)
            progress(result.inspected, rec)
    except Cancelled:
        result.cancelled = True
    return result


def discover_bandizip() -> str:
    paths = []
    found = shutil.which('bz.exe')
    if found:
        paths.append(Path(found))
    if os.name == 'nt':
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Bandizip.exe',
                                        0, winreg.KEY_READ | view) as key:
                        paths.append(Path(winreg.QueryValue(key, None).strip('"')).with_name('bz.exe'))
                except OSError:
                    pass
    for env in ('ProgramW6432', 'ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA'):
        if os.environ.get(env):
            paths.append(Path(os.environ[env]) / 'Bandizip' / 'bz.exe')
    return next((str(p) for p in paths if p.is_file()), '')


def data_dir():
    base = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'NASComicCompressor' / 'logs'
    base.mkdir(parents=True, exist_ok=True)
    return base


class Audit:
    """Append and flush each event so completed work survives interruption."""
    def __init__(self, directory=None):
        directory = Path(directory) if directory else data_dir()
        directory.mkdir(parents=True, exist_ok=True)
        token = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8]
        self.csv_path = directory / (token + '.csv')
        self.log_path = directory / (token + '.log')
        self.csv_file = self.csv_path.open('w', encoding='utf-8-sig', newline='')
        self.log_file = self.log_path.open('w', encoding='utf-8')
        self.writer = csv.writer(self.csv_file)
        self.writer.writerow(['시간', '원본 경로', '상태', '설명', '이미지 수', '원본 바이트', '출력 경로', '출력 바이트', '원본 처리'])
        self.csv_file.flush()
        from . import __version__
        self.log(f'NAS Comic Compressor {__version__} · Python {sys.version.split()[0]} · {os.name}')

    def record(self, rec):
        # Prefix formula-like cells to prevent spreadsheet formula execution.
        def safe(value):
            return "'" + value if isinstance(value, str) and value.startswith(('=', '+', '-', '@', '\t', '\r')) else value
        self.writer.writerow([datetime.now().isoformat(timespec='seconds')] +
                             [safe(getattr(rec, k)) for k in Record.__dataclass_fields__])
        self.csv_file.flush()

    def log(self, message):
        self.log_file.write(f'{datetime.now().isoformat(timespec="seconds")} {message}\n')
        self.log_file.flush()

    def close(self):
        self.csv_file.close()
        self.log_file.close()


class ProcessJob:
    """Windows job closes all assigned bz processes if this app exits/crashes."""
    def __init__(self, process):
        self.handle = None
        if os.name != 'nt':
            return
        import ctypes as c
        from ctypes import wintypes as w

        class Basic(c.Structure):
            _fields_ = [('ProcessTime', c.c_int64), ('JobTime', c.c_int64), ('Flags', w.DWORD),
                        ('MinWorking', c.c_size_t), ('MaxWorking', c.c_size_t), ('Active', w.DWORD),
                        ('Affinity', c.c_size_t), ('Priority', w.DWORD), ('Scheduling', w.DWORD)]

        class IO(c.Structure):
            _fields_ = [(name, c.c_uint64) for name in ('ReadOps', 'WriteOps', 'OtherOps', 'ReadBytes', 'WriteBytes', 'OtherBytes')]

        class Extended(c.Structure):
            _fields_ = [('Basic', Basic), ('IO', IO), ('ProcessMemory', c.c_size_t),
                        ('JobMemory', c.c_size_t), ('PeakProcess', c.c_size_t), ('PeakJob', c.c_size_t)]

        self.kernel = c.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        info = Extended()
        info.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle or not self.kernel.SetInformationJobObject(self.handle, 9, c.byref(info), c.sizeof(info)):
            self.close()
            raise OSError(c.get_last_error(), '프로세스 안전 종료 Job 설정 실패')
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            self.close()
            raise OSError(c.get_last_error(), '반디집 프로세스 Job 할당 실패')

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def run_bz(args, cwd, control, log):
    control.check()
    log('반디집: ' + subprocess.list2cmdline(args))
    # Disk spool avoids stdout pipe deadlocks and unbounded RAM on large archives.
    with tempfile.TemporaryFile() as spool:
        process = subprocess.Popen(args, cwd=str(cwd), shell=False, stdin=subprocess.DEVNULL,
                                   stdout=spool, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        job = None
        try:
            job = ProcessJob(process)
            while process.poll() is None:
                control.check(allow_pause=False)
                control.cancelled.wait(0.1)
            control.check(allow_pause=False)
            spool.seek(0, os.SEEK_END)
            length = spool.tell()
            spool.seek(max(0, length - 16384))
            tail = spool.read().decode('utf-8', errors='replace')
            log(tail.strip())
            if process.returncode != 0:
                raise RuntimeError(f'반디집 종료 코드 {process.returncode}: {tail[-1500:]}')
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            if job:
                job.close()


def batches(names, budget=20000):
    batch, length = [], 0
    for name in names:
        arg = '.\\' + name if os.name == 'nt' else './' + name
        cost = len(subprocess.list2cmdline([arg]).encode('utf-16-le')) // 2 + 1
        if length + cost > budget and batch:
            yield batch
            batch, length = [], 0
        if cost > budget:
            raise ValueError('파일명이 명령줄 제한을 초과합니다')
        batch.append(arg)
        length += cost
    if batch:
        yield batch


def hash_stream(stream, control):
    digest = hashlib.sha256()
    while True:
        control.check()
        block = stream.read(1024 * 1024)
        if not block:
            return digest.digest()
        digest.update(block)


def revalidate(candidate, control):
    images, dirs, _ = snapshot(candidate.path, control)
    if dirs or images != candidate.files:
        raise RuntimeError('스캔 후 원본 파일 목록/크기/수정 시각이 변경되었습니다. 다시 검색하세요.')


def retry_access(operation, control, log, label):
    """Retry only access/sharing errors, never data errors or name collisions."""
    for attempt in range(len(ACCESS_RETRY_DELAYS) + 1):
        control.check()
        try:
            return operation()
        except OSError as exc:
            retryable = isinstance(exc, PermissionError) or getattr(exc, 'winerror', None) in (5, 32, 33)
            if not retryable or attempt == len(ACCESS_RETRY_DELAYS):
                raise
            delay = ACCESS_RETRY_DELAYS[attempt]
            log(f'접근 재시도 {attempt + 1}/{len(ACCESS_RETRY_DELAYS)} · {label} · {delay:g}초 대기 · '
                f'errno={exc.errno}, winerror={getattr(exc, "winerror", None)}: {exc}')
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                control.check()
                control.cancelled.wait(min(0.1, max(0, deadline - time.monotonic())))


@contextmanager
def archive_workspace(parent, log):
    """Clean up our known file only; cleanup errors never mask the real result."""
    parent = Path(parent)
    temp = Path(tempfile.mkdtemp(prefix='.ncc-', suffix='.tmp', dir=parent))
    try:
        yield temp
    finally:
        try:
            if is_link(temp.lstat()) or temp.resolve().parent != parent.resolve():
                raise OSError('임시 디렉터리 경로/속성이 변경되어 정리를 보류합니다')
            archive = temp / 'result.zip'
            cleanup_control = Control()
            if archive.exists():
                retry_access(archive.unlink, cleanup_control, log, '임시 ZIP 정리')
            # Do not recurse into unknown files, or change NAS permissions/attributes.
            retry_access(temp.rmdir, cleanup_control, log, '빈 임시 디렉터리 정리')
        except Exception as exc:
            log(f'임시 정리 보류: {temp}: {exc} · 원래 작업 결과는 유지합니다')


def sync_archive(archive, control, log):
    try:
        stream = retry_access(lambda: archive.open('r+b'), control, log, 'ZIP 동기화용 쓰기 열기')
    except PermissionError as exc:
        # Contents have already passed Bandizip and SHA-256 checks. Publish without
        # changing ACL/read-only attributes, but absolutely disallow source deletion.
        log(f'추가 쓰기 열기 거부: {archive}: {exc} · 검증된 ZIP 확정은 시도하되 원본은 보존합니다')
        return False
    with stream:
        stream.flush()
        os.fsync(stream.fileno())  # A genuine flush/I/O failure must still fail the job.
    return True


def compress(candidate: Candidate, bz: str, control: Control,
             log: Callable = lambda *_: None, progress: Callable = lambda *_: None,
             level=0, verify_images=True, runner=run_bz, delete_originals=False) -> Record:
    """Publish a verified ZIP; source removal requires explicit opt-in."""
    if level not in (0, 1):
        raise ValueError('압축 수준은 0 또는 1이어야 합니다')
    stage = '원본 목록 재확인'
    def phase(name):
        nonlocal stage
        stage = name
        progress(name)
        log('단계: ' + name)
    try:
        control.check()
        revalidate(candidate, control)
        if existing_archive(candidate.output):
            return Record(str(candidate.path), '건너뜀', '동일 이름 압축파일 존재',
                          len(candidate.files), candidate.size, str(candidate.output))
        # Private sibling directory means same-volume final rename, including SMB.
        phase('임시 작업 디렉터리 생성')
        with archive_workspace(candidate.output.parent, log) as temp:
            archive = Path(temp) / 'result.zip'
            source_hashes = {}
            phase('원본 이미지 구조 및 SHA-256 검사')
            for index, item in enumerate(candidate.files):
                control.check()
                source = candidate.path / item.name
                progress(f'원본 검사 {index + 1}/{len(candidate.files)}: {item.name}')
                if verify_images:
                    from PIL import Image
                    try:
                        with Image.open(source) as image:
                            image.verify()
                    except Exception as exc:
                        raise RuntimeError(f'이미지 구조 검사 실패: {item.name}: {exc}') from exc
                with source.open('rb') as stream:
                    source_hashes[item.name] = hash_stream(stream, control)
            revalidate(candidate, control)
            phase('반디집 ZIP 압축')
            for index, group in enumerate(batches([item.name for item in candidate.files])):
                control.check()
                progress(f'반디집 압축 중 (묶음 {index + 1}, 완료 후 일시정지 가능)')
                runner([bz, 'c' if index == 0 else 'a', '-fmt:zip', f'-l:{level}', '-r-',
                        '-consolemode:utf8', str(archive), *group], candidate.path, control, log)
            phase('반디집 무결성 검사')
            runner([bz, 't', '-consolemode:utf8', str(archive)], candidate.path, control, log)
            phase('ZIP 내부 목록 및 SHA-256 검증')
            with retry_access(lambda: archive.open('rb'), control, log, '검증용 ZIP 읽기 열기') as archive_stream, \
                    zipfile.ZipFile(archive_stream) as zipped:
                infos = zipped.infolist()
                if len(infos) != len(candidate.files) or {i.filename for i in infos} != set(source_hashes):
                    raise RuntimeError('압축 내부 파일 목록이 원본과 다릅니다 (경로/파일명 포함)')
                for index, info in enumerate(infos):
                    progress(f'내용 해시 검증 {index + 1}/{len(infos)}: {info.filename}')
                    control.check()
                    with zipped.open(info) as stream:
                        if hash_stream(stream, control) != source_hashes[info.filename]:
                            raise RuntimeError(f'압축 내용 SHA-256 불일치: {info.filename}')
                    # Check current source as well: reject writes during compression.
                    with (candidate.path / info.filename).open('rb') as stream:
                        if hash_stream(stream, control) != source_hashes[info.filename]:
                            raise RuntimeError(f'작업 도중 원본 내용 변경: {info.filename}')
            revalidate(candidate, control)
            control.check()
            size = archive.stat().st_size
            phase('ZIP 저장 동기화')
            synced = sync_archive(archive, control, log)
            if existing_archive(candidate.output):
                raise FileExistsError('작업 도중 동일 이름 압축파일이 생성되었습니다')
            phase('최종 ZIP 이름 확정 (덮어쓰기 금지)')
            if os.name != 'nt':
                # Atomic no-clobber publication on POSIX (Windows is the shipped target).
                os.link(archive, candidate.output)
                archive.unlink()
            else:
                retry_access(lambda: os.rename(archive, candidate.output), control, log, '최종 ZIP 이름 확정')
        rec = Record(str(candidate.path), '성공', '반디집 검사 및 SHA-256 검증 통과; 원본 보존',
                     len(candidate.files), candidate.size, str(candidate.output), size)
        if not synced:
            rec.detail += '; 추가 쓰기 열기가 거부되어 저장 동기화 확인 불가, 원본 보존'
            if delete_originals:
                rec.original_action = '삭제 보류'
            log(f'원본 보존: {candidate.path}: 저장 동기화 확인 불가')
        if delete_originals and synced:
            try:
                from .deletion import delete_verified_originals, PartialDeletionError
                action, detail = delete_verified_originals(candidate, control, progress)
                rec.original_action = action
                rec.detail = '반디집 검사 및 SHA-256 검증 통과; ' + detail
            except Cancelled:
                rec.detail += '; 중단 요청으로 원본 삭제 안 함'
            except Exception as exc:
                rec.original_action = '삭제 오류·원본 확인' if isinstance(exc, PartialDeletionError) else '삭제 보류'
                rec.detail += f'; 삭제 보류: {exc}'
            log(f'원본 처리: {rec.path}: {rec.original_action}: {rec.detail}')
        return rec
    except Cancelled as exc:
        return Record(str(candidate.path), '취소', str(exc), len(candidate.files), candidate.size, str(candidate.output))
    except Exception as exc:
        detail = f'{stage}: {exc} (errno={getattr(exc, "errno", None)}, winerror={getattr(exc, "winerror", None)})'
        log(f'실패: {candidate.path}: {detail}\n{traceback.format_exc()}')
        return Record(str(candidate.path), '실패', detail, len(candidate.files), candidate.size, str(candidate.output))
