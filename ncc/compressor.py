"""압축 → 검사 → 확정 → (선택) 원본 삭제 파이프라인 (GUI/Qt 비의존)."""
from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
import zipfile
from dataclasses import dataclass, field
from typing import Callable

from . import rules
from .bandizip import BandizipRunner
from .scanner import Candidate

LogFn = Callable[[str], None]

# 상태 문자열
ST_SUCCESS = "성공"
ST_FAIL = "실패"
ST_SKIP = "건너뜀"
ST_CANCEL = "취소"
ST_NOTRUN = "미실행"


@dataclass
class Options:
    level: int = 0                 # 0=저장, 1=빠른, 5=표준
    delete_originals: bool = False
    verify_hash: bool = True       # 원본 삭제 시 SHA-256 비교 (삭제 시 항상 강제)
    check_headers: bool = True     # 이미지 앞 16바이트 매직넘버 검사 (경고만)
    max_consecutive_failures: int = 10
    lock_retry_seconds: float = 60.0  # 파일 잠금(동기화/백신) 해제 대기 최대 시간


@dataclass
class ItemResult:
    candidate: Candidate
    status: str
    message: str
    output_path: str = ""
    output_bytes: int = 0
    original_action: str = "보존"
    warnings: list[str] = field(default_factory=list)


class ItemFailed(Exception):
    pass


# ----------------------------------------------------------------------
# 파일 잠금 대응: NAS 동기화 클라이언트(.sync 등)/백신이 새 파일을 잠시 잡고 있으면
# PermissionError(Errno 13 / WinError 32)가 난다. 일정 시간 재시도한다.
def retry_locked(fn, *, total: float, cancel: threading.Event | None, log: LogFn | None, what: str):
    delay = 0.25
    deadline = time.monotonic() + total
    attempt = 0
    while True:
        try:
            return fn()
        except PermissionError as e:
            attempt += 1
            if time.monotonic() + delay > deadline:
                raise
            if log and attempt in (1, 4, 8, 16):
                log(f"  · {what}: 파일이 잠겨 있음 → 재시도 {attempt}회 ({e.strerror or e})")
            if cancel is not None and cancel.wait(delay):
                raise
            delay = min(delay * 1.6, 5.0)


def rename_noreplace(src: str, dst: str) -> None:
    """dst가 있으면 절대 덮어쓰지 않는 이름 변경."""
    if os.name == "nt":
        os.rename(src, dst)  # Windows: 대상이 있으면 FileExistsError
    else:
        os.link(src, dst)    # POSIX: 대상이 있으면 FileExistsError
        os.unlink(src)


def _same_stem_archive_exists(parent: str, name: str) -> str | None:
    try:
        with os.scandir(parent) as it:
            for e in it:
                st = rules.archive_stem(e.name)
                if st is not None and st == name.lower():
                    return e.name
    except OSError:
        return None
    return None


def _list_dir_state(path: str):
    files: dict[str, tuple[int, int]] = {}
    dirs: list[str] = []
    with os.scandir(path) as it:
        for e in it:
            if e.is_dir(follow_symlinks=False):
                dirs.append(e.name)
            else:
                st = e.stat(follow_symlinks=False)
                files[e.name] = (st.st_size, st.st_mtime_ns)
    return files, dirs


def _sha256_file(path: str, cancel) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            if cancel is not None and cancel.is_set():
                raise ItemFailed("사용자가 중단했습니다")
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


class Compressor:
    def __init__(self, runner: BandizipRunner, options: Options,
                 cancel_event: threading.Event | None = None,
                 log: LogFn | None = None,
                 file_progress: Callable[[int, int], None] | None = None):
        self.runner = runner
        self.opt = options
        self.cancel = cancel_event or threading.Event()
        self.log = log or (lambda s: None)
        self.file_progress = file_progress

    # ------------------------------------------------------------------
    def process(self, c: Candidate) -> ItemResult:
        tmp_path = os.path.join(c.parent, f".ncc-{secrets.token_hex(8)}.tmp.zip")
        res = ItemResult(c, ST_FAIL, "", output_path=c.output_path)
        try:
            self._process(c, tmp_path, res)
        except _Skip:
            pass  # res.status/message 는 이미 설정됨
        except ItemFailed as e:
            res.status = ST_CANCEL if self.cancel.is_set() else ST_FAIL
            res.message = str(e)
        except OSError as e:
            res.status = ST_CANCEL if self.cancel.is_set() else ST_FAIL
            res.message = f"파일 오류: {e}"
        except Exception as e:  # 예기치 못한 오류도 항목 단위 실패로 처리
            res.status = ST_FAIL
            res.message = f"예기치 못한 오류: {type(e).__name__}: {e}"
        finally:
            if os.path.lexists(tmp_path):
                try:
                    retry_locked(lambda: os.remove(tmp_path), total=20, cancel=None, log=None, what="임시파일 삭제")
                except OSError as e:
                    self.log(f"  ! 임시파일을 지우지 못함 (나중에 '임시파일 정리'로 삭제 가능): {tmp_path} ({e})")
        return res

    # ------------------------------------------------------------------
    def _check_cancel(self):
        if self.cancel.is_set():
            raise ItemFailed("사용자가 중단했습니다")

    def _process(self, c: Candidate, tmp: str, res: ItemResult) -> None:
        self._check_cancel()
        # 1) 스캔 이후 변경 여부 재확인
        if not os.path.isdir(c.path):
            raise ItemFailed("원본 폴더에 접근할 수 없음 (삭제/이동/네트워크 끊김)")
        if os.path.lexists(c.output_path):
            res.status = ST_SKIP
            res.message = "출력 파일이 이미 있음 (덮어쓰지 않음)"
            raise _Skip()
        other = _same_stem_archive_exists(c.parent, c.name)
        if other:
            res.status = ST_SKIP
            res.message = f"같은 이름의 압축파일이 이미 있음: {other}"
            raise _Skip()
        files, dirs = _list_dir_state(c.path)
        expected = {i.name: i for i in c.images}
        now_images = {n for n in files if rules.is_image_name(n)}
        if now_images != set(expected):
            raise ItemFailed("검색 이후 폴더 내용이 바뀜 → 다시 검색하세요")
        for n, img in expected.items():
            if files[n][0] != img.size:
                raise ItemFailed(f"검색 이후 파일 크기가 바뀜: {n} → 다시 검색하세요")

        # 2) (경고용) 이미지 헤더 검사 - 디코딩 없이 앞 16바이트만
        if self.opt.check_headers:
            bad = []
            for n in expected:
                self._check_cancel()
                try:
                    with open(os.path.join(c.path, n), "rb") as f:
                        head = f.read(16)
                    if not rules.header_looks_valid(n, head):
                        bad.append(n)
                except OSError as e:
                    raise ItemFailed(f"이미지 파일을 읽을 수 없음: {n} ({e.strerror or e})")
            if bad:
                w = f"헤더가 확장자와 다른 이미지 {len(bad)}개 (손상/확장자 오류 의심): " + ", ".join(bad[:5])
                res.warnings.append(w)
                self.log("  ⚠ " + w)

        # 3) 반디집 압축 → 임시파일
        names = sorted(expected, key=str.lower)
        total = len(names)
        done = [0]

        def on_line(s: str):
            if s.startswith("Compressing:") or s.startswith("Adding:"):
                done[0] += 1
                if self.file_progress:
                    self.file_progress(min(done[0], total), total)

        if self.file_progress:
            self.file_progress(0, total)
        r = self.runner.create_zip(tmp, names, cwd=c.path, level=self.opt.level,
                                   cancel_event=self.cancel, on_line=on_line)
        self._check_cancel()
        if r.returncode != 0:
            raise ItemFailed(f"반디집 압축 실패 (코드 {r.returncode}): {_tail(r.output)}")
        if not os.path.isfile(tmp):
            raise ItemFailed("반디집이 압축파일을 만들지 않았습니다")
        if self.file_progress:
            self.file_progress(total, total)

        # 4) 반디집 무결성 검사
        t = self.runner.test(tmp, cancel_event=self.cancel)
        self._check_cancel()
        if t.returncode != 0:
            raise ItemFailed(f"반디집 무결성 검사 실패 (코드 {t.returncode}): {_tail(t.output)}")

        # 5) 내부 목록/크기 검증 (+ 삭제 시 해시)
        need_hash = self.opt.delete_originals
        hashes = retry_locked(lambda: self._verify_zip(tmp, c, need_hash),
                              total=self.opt.lock_retry_seconds, cancel=self.cancel,
                              log=self.log, what="압축파일 검증")

        # 6) 최종 이름으로 확정 (덮어쓰기 금지)
        if os.path.lexists(c.output_path):
            res.status = ST_SKIP
            res.message = "작업 중 같은 이름의 출력 파일이 생김 (덮어쓰지 않음)"
            raise _Skip()
        self._check_cancel()
        try:
            retry_locked(lambda: rename_noreplace(tmp, c.output_path),
                         total=self.opt.lock_retry_seconds, cancel=self.cancel,
                         log=self.log, what="최종 파일 이름 변경")
        except FileExistsError:
            res.status = ST_SKIP
            res.message = "같은 이름의 출력 파일이 이미 있음 (덮어쓰지 않음)"
            raise _Skip()
        res.output_bytes = os.path.getsize(c.output_path)
        res.status = ST_SUCCESS
        res.message = "압축 및 무결성 검사 완료"

        # 7) 원본 삭제 (옵션)
        if self.opt.delete_originals:
            res.original_action = self._delete_original(c, hashes)

    # ------------------------------------------------------------------
    def _verify_zip(self, archive: str, c: Candidate, compute_hash: bool) -> dict[str, str]:
        expected = {i.name: i.size for i in c.images}
        hashes: dict[str, str] = {}
        with zipfile.ZipFile(archive) as z:
            infos = z.infolist()
            names = [i.filename for i in infos]
            if len(names) != len(set(names)):
                raise ItemFailed("압축파일 내부에 중복 이름이 있음")
            for i in infos:
                n = i.filename
                if i.is_dir() or "/" in n or "\\" in n or ":" in n:
                    raise ItemFailed(f"압축파일 내부 경로가 올바르지 않음: {n!r}")
            if set(names) != set(expected):
                missing = sorted(set(expected) - set(names))[:3]
                extra = sorted(set(names) - set(expected))[:3]
                hint = ""
                if extra and all(not rules.is_image_name(x) or "?" in x or "�" in x for x in extra):
                    hint = " (파일명 인코딩 문제일 수 있음: 반디집 설정 > 압축 > 'UTF-8 파일명 사용' 확인)"
                raise ItemFailed(f"압축파일 내부 목록이 원본과 다름. 누락={missing} 추가={extra}{hint}")
            for i in infos:
                if i.file_size != expected[i.filename]:
                    raise ItemFailed(f"압축파일 내부 크기가 원본과 다름: {i.filename}")
            if compute_hash:
                for i in infos:
                    self._check_cancel()
                    h = hashlib.sha256()
                    with z.open(i) as f:  # 읽는 동안 CRC도 함께 검사됨
                        while True:
                            b = f.read(1024 * 1024)
                            if not b:
                                break
                            h.update(b)
                    hashes[i.filename] = h.hexdigest()
        return hashes

    def _delete_original(self, c: Candidate, zip_hashes: dict[str, str]) -> str:
        """검증된 경우에만 원본 폴더 삭제. 실패하면 보존하고 사유 반환."""
        if not c.deletable_layout:
            self.log("  · 원본 보존: 이미지 외 파일/하위 폴더가 있어 자동 삭제 금지")
            return "보존 (이미지 외 파일 있음)"
        if self.cancel.is_set():
            return "보존 (중단)"
        try:
            files, dirs = _list_dir_state(c.path)
        except OSError as e:
            return f"보존 (폴더 재확인 실패: {e})"
        expected = {i.name: i for i in c.images}
        if dirs or set(files) != set(expected):
            return "보존 (폴더 내용이 바뀜)"
        for n, img in expected.items():
            if files[n] != (img.size, img.mtime_ns):
                return f"보존 (파일 변경됨: {n})"
        if set(zip_hashes) != set(expected):
            return "보존 (해시 검증 불가)"
        for n in expected:
            if self.cancel.is_set():
                return "보존 (중단)"
            try:
                h = _sha256_file(os.path.join(c.path, n), self.cancel)
            except (OSError, ItemFailed) as e:
                return f"보존 (원본 해시 계산 실패: {e})"
            if h != zip_hashes[n]:
                return f"보존 (해시 불일치: {n})"
        # 모든 검증 통과 → 삭제
        deleted = 0
        for n in expected:
            try:
                os.remove(os.path.join(c.path, n))
                deleted += 1
            except OSError as e:
                self.log(f"  ! 원본 일부 삭제 실패 ({deleted}/{len(expected)}), 압축파일은 검증 완료: {e}")
                return f"일부 삭제 ({deleted}/{len(expected)})"
        try:
            os.rmdir(c.path)
        except OSError as e:
            return f"이미지 삭제, 폴더 남음 ({e.strerror or e})"
        return "삭제 (해시 검증 완료)"


class _Skip(ItemFailed):
    pass


def _tail(s: str, n: int = 300) -> str:
    s = (s or "").strip().replace("\n", " | ")
    return s[-n:]


# ----------------------------------------------------------------------
def find_temp_artifacts(root: str) -> list[str]:
    """이 프로그램(및 이전 버전)이 남긴 임시파일/폴더 찾기."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        for f in filenames:
            if rules.TEMP_FILE_RE.match(f):
                found.append(os.path.join(dirpath, f))
        keep = []
        for d in dirnames:
            if rules.LEGACY_TEMP_DIR_RE.match(d):
                found.append(os.path.join(dirpath, d))
            elif not rules.is_ignored_dir(d):
                keep.append(d)
        dirnames[:] = keep
    return found


def remove_temp_artifact(path: str) -> None:
    """임시 산출물만 삭제. 이전 버전 임시폴더는 result.zip 외 파일이 있으면 삭제하지 않음."""
    name = os.path.basename(path)
    if os.path.isfile(path) and rules.TEMP_FILE_RE.match(name):
        os.remove(path)
        return
    if os.path.isdir(path) and rules.LEGACY_TEMP_DIR_RE.match(name):
        entries = os.listdir(path)
        if any(e.lower() not in ("result.zip",) for e in entries):
            raise OSError(f"예상하지 못한 파일이 있어 삭제하지 않음: {entries[:5]}")
        for e in entries:
            os.remove(os.path.join(path, e))
        os.rmdir(path)
        return
    raise OSError("임시 산출물 형식이 아님")
