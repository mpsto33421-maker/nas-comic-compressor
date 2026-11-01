"""이미 압축이 끝났는데 남아 있는 원본 폴더 정리 (GUI/Qt 비의존).

대상: 이미지가 직접 들어 있는 폴더 X 옆에 같은 이름의 X.zip / X.cbz 가 있는 경우.
삭제 조건 (하나라도 어긋나면 그 폴더는 보존):
  1. 폴더 안에 하위 폴더가 없고, 이미지 외 파일은 Thumbs.db / desktop.ini 같은 시스템 찌꺼기만 있음
  2. 압축파일을 열 수 있음 (ZIP/CBZ만 검증 가능. RAR/7Z 는 건너뜀)
  3. 폴더의 모든 이미지가 압축파일 안에 같은 이름으로 있음
  4. 크기가 같고, 원본과 압축파일 내부 파일의 SHA-256 이 모두 같음 (CRC 검사도 함께 수행)
  5. 검증 후 삭제 직전에 원본이 바뀌지 않았음 (크기·수정시각 재확인)
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
import zipfile
from dataclasses import dataclass, field
from typing import Callable

from . import rules

JUNK_FILES = frozenset({"thumbs.db", "desktop.ini", ".ds_store", "ehthumbs.db"})
VERIFIABLE_EXTS = (".zip", ".cbz")


@dataclass
class LeftoverCandidate:
    folder: str
    archive: str
    images: list[str]
    total_bytes: int
    checked: bool = True
    status: str = "대기"
    message: str = ""


@dataclass
class LeftoverScan:
    candidates: list[LeftoverCandidate] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    dirs_scanned: int = 0
    cancelled: bool = False


def _retry(fn, cancel: threading.Event | None, total: float = 30.0):
    delay, deadline = 0.25, time.monotonic() + total
    while True:
        try:
            return fn()
        except PermissionError:
            if time.monotonic() + delay > deadline:
                raise
            if cancel is not None and cancel.wait(delay):
                raise
            delay = min(delay * 1.6, 4.0)


def find_leftovers(root: str, cancel: threading.Event | None = None,
                   progress: Callable[[int, str], None] | None = None) -> LeftoverScan:
    """압축파일이 이미 있는데 원본 이미지 폴더가 남아 있는 곳을 찾는다 (메타데이터만 사용)."""
    res = LeftoverScan()
    cancel = cancel or threading.Event()
    root = os.path.abspath(root)

    def walk(path: str, sibling_files: dict[str, str] | None, is_root: bool):
        if cancel.is_set():
            res.cancelled = True
            return
        res.dirs_scanned += 1
        if progress and (res.dirs_scanned % 25 == 0 or res.dirs_scanned == 1):
            progress(res.dirs_scanned, path)
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except OSError as e:
            res.skipped.append((path, f"폴더를 읽을 수 없음: {e.strerror or e}"))
            return
        files: dict[str, str] = {}   # 소문자 이름 -> 실제 이름
        subdirs = []
        images, others = [], []
        size = 0
        for e in entries:
            try:
                if e.is_symlink():
                    continue
                if e.is_dir(follow_symlinks=False):
                    if not rules.is_ignored_dir(e.name):
                        subdirs.append(e)
                    continue
                if not e.is_file(follow_symlinks=False):
                    continue
                files[e.name.lower()] = e.name
                if rules.is_image_name(e.name):
                    images.append(e.name)
                    size += e.stat(follow_symlinks=False).st_size
                elif e.name.lower() not in JUNK_FILES and not rules.TEMP_FILE_RE.match(e.name):
                    others.append(e.name)
            except OSError:
                others.append(e.name)
        for sd in sorted(subdirs, key=lambda d: d.name.lower()):
            walk(sd.path, files, False)
            if res.cancelled:
                return
        if is_root or not images or sibling_files is None:
            return
        name = os.path.basename(path.rstrip("\\/"))
        archive = None
        for ext in VERIFIABLE_EXTS:
            real = sibling_files.get((name + ext).lower())
            if real:
                archive = os.path.join(os.path.dirname(path.rstrip("\\/")), real)
                break
        if archive is None:
            return
        if subdirs:
            res.skipped.append((path, "하위 폴더가 있어 정리 대상에서 제외"))
            return
        if others:
            res.skipped.append((path, f"이미지 외 파일 {len(others)}개가 있어 제외: {', '.join(others[:3])}"))
            return
        res.candidates.append(LeftoverCandidate(path, archive, sorted(images, key=str.lower), size))

    walk(root, None, True)
    res.candidates.sort(key=lambda c: c.folder.lower())
    return res


class VerifyFailed(Exception):
    pass


def _sha256_stream(f, cancel) -> str:
    h = hashlib.sha256()
    while True:
        if cancel is not None and cancel.is_set():
            raise VerifyFailed("사용자가 중단했습니다")
        b = f.read(1024 * 1024)
        if not b:
            return h.hexdigest()
        h.update(b)


def verify_and_delete(c: LeftoverCandidate, cancel: threading.Event | None = None,
                      log: Callable[[str], None] | None = None,
                      dry_run: bool = False) -> tuple[bool, str]:
    """검증에 모두 통과한 경우에만 원본 폴더를 삭제. (성공 여부, 메시지)"""
    log = log or (lambda s: None)
    try:
        if not os.path.isdir(c.folder):
            return False, "폴더가 이미 없음"
        if not os.path.isfile(c.archive):
            return False, "압축파일이 없음"
        # 1) 폴더 상태 재확인
        state: dict[str, tuple[int, int]] = {}
        junk: list[str] = []
        with os.scandir(c.folder) as it:
            for e in it:
                if e.is_dir(follow_symlinks=False):
                    return False, "하위 폴더가 생겨 보존"
                if rules.is_image_name(e.name):
                    st = e.stat(follow_symlinks=False)
                    state[e.name] = (st.st_size, st.st_mtime_ns)
                elif e.name.lower() in JUNK_FILES:
                    junk.append(e.name)
                else:
                    return False, f"이미지 외 파일이 있어 보존: {e.name}"
        if not state:
            return False, "이미지가 없음"

        # 2) 압축파일 열기 + 이름 매칭
        def check_zip():
            with zipfile.ZipFile(c.archive) as z:
                by_name: dict[str, list[zipfile.ZipInfo]] = {}
                for info in z.infolist():
                    if info.is_dir():
                        continue
                    base = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
                    by_name.setdefault(base, []).append(info)
                missing = [n for n in state if n not in by_name]
                if missing:
                    raise VerifyFailed(f"압축파일에 없는 이미지 {len(missing)}개 → 보존: {', '.join(missing[:3])}")
                dup = [n for n in state if len(by_name[n]) > 1]
                if dup:
                    raise VerifyFailed(f"압축파일 안에 같은 이름이 여러 개 → 보존: {dup[0]}")
                for n, (sz, _) in state.items():
                    if by_name[n][0].file_size != sz:
                        raise VerifyFailed(f"크기가 다름 → 보존: {n}")
                # 3) SHA-256 비교 (zip 읽기 중 CRC도 검사됨)
                for n in sorted(state, key=str.lower):
                    with z.open(by_name[n][0]) as zf:
                        hz = _sha256_stream(zf, cancel)
                    with open(os.path.join(c.folder, n), "rb") as of:
                        ho = _sha256_stream(of, cancel)
                    if hz != ho:
                        raise VerifyFailed(f"내용이 다름 → 보존: {n}")
        try:
            _retry(check_zip, cancel)
        except zipfile.BadZipFile:
            return False, "압축파일이 손상됐거나 ZIP 형식이 아님 → 보존"
        except VerifyFailed as e:
            return False, str(e)

        if dry_run:
            return True, f"검증 통과 (이미지 {len(state)}개) — 시험 실행이라 삭제하지 않음"
        if cancel is not None and cancel.is_set():
            return False, "사용자가 중단했습니다"

        # 4) 삭제 직전 재확인
        for n, (sz, mt) in state.items():
            st = os.stat(os.path.join(c.folder, n))
            if (st.st_size, st.st_mtime_ns) != (sz, mt):
                return False, f"검증 중 파일이 바뀌어 보존: {n}"
        # 5) 삭제
        deleted = 0
        for n in list(state) + junk:
            try:
                os.remove(os.path.join(c.folder, n))
                deleted += 1
            except OSError as e:
                return False, f"일부만 삭제됨 ({deleted}개, 압축파일 검증 완료): {e.strerror or e}"
        try:
            os.rmdir(c.folder)
        except OSError as e:
            return True, f"이미지 {len(state)}개 삭제, 폴더는 남음 ({e.strerror or e})"
        return True, f"검증 후 삭제 완료 (이미지 {len(state)}개)"
    except VerifyFailed as e:
        return False, str(e)
    except OSError as e:
        return False, f"파일 오류 → 보존: {e.strerror or e}"
