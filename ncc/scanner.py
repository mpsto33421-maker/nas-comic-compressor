"""압축 대상 자동 판별 (GUI/Qt 비의존).

판별 규칙 요약
- 직접 이미지가 있고, 하위 폴더 트리 어디에도 이미지가 없는 폴더 = 압축 대상(말단 폴더)
- 하위 폴더 트리에 이미지가 있는 폴더 = 상위(작품) 폴더 → 통째로 압축하지 않음.
  직접 들어 있는 이미지(cover.jpg 등)는 건드리지 않고 그대로 보존
- 하위 폴더를 읽지 못하면(권한/네트워크 오류) 상위 폴더도 '불확실'로 보고 압축하지 않음
- 같은 위치에 '<폴더명>.<압축확장자>' 가 하나라도 있으면 제외 (zip/cbz/rar/7z ...)
- 작업 루트 폴더 자체는 압축하지 않음
- 심볼릭 링크/정션은 따라가지 않음
- '.'으로 시작하는 폴더, NAS 시스템 폴더(@eaDir, #recycle ...)는 탐색하지 않음
- 파일 메타데이터(os.scandir)만 사용, 이미지 디코딩 없음
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Callable

from . import rules


class ScanCancelled(Exception):
    pass


@dataclass(frozen=True)
class ImageFile:
    name: str
    size: int
    mtime_ns: int


@dataclass
class Candidate:
    path: str
    parent: str
    name: str
    images: list[ImageFile]
    other_files: list[str]
    empty_subdirs: list[str]
    output_path: str
    warnings: list[str] = field(default_factory=list)
    checked: bool = True
    status: str = "대기"

    @property
    def image_count(self) -> int:
        return len(self.images)

    @property
    def total_bytes(self) -> int:
        return sum(i.size for i in self.images)

    @property
    def deletable_layout(self) -> bool:
        """원본 삭제가 구조적으로 허용되는지 (이미지 외 파일/하위폴더 없음)."""
        return not self.other_files and not self.empty_subdirs


@dataclass
class ScanRecord:
    path: str
    status: str  # '대상' | '건너뜀' | '오류'
    reason: str


@dataclass
class ScanResult:
    root: str
    candidates: list[Candidate] = field(default_factory=list)
    records: list[ScanRecord] = field(default_factory=list)
    dirs_scanned: int = 0
    temp_artifacts: list[str] = field(default_factory=list)
    cancelled: bool = False

    @property
    def skipped(self) -> list[ScanRecord]:
        return [r for r in self.records if r.status == "건너뜀"]

    @property
    def errors(self) -> list[ScanRecord]:
        return [r for r in self.records if r.status == "오류"]


@dataclass
class _Subtree:
    has_images: bool
    uncertain: bool


def _is_link(entry: os.DirEntry) -> bool:
    try:
        if entry.is_symlink():
            return True
        is_junction = getattr(entry, "is_junction", None)
        return bool(is_junction and is_junction())
    except OSError:
        return True


class Scanner:
    def __init__(
        self,
        root: str,
        output_ext: str = ".cbz",
        cancel_event: threading.Event | None = None,
        progress: Callable[[int, str], None] | None = None,
        record_empty_dirs: bool = False,
        min_images: int = 2,
        skip_cover_only: bool = True,
        cover_with_archives_max: int = 5,
    ) -> None:
        self.root = os.path.abspath(root)
        self.output_ext = output_ext if output_ext.startswith(".") else "." + output_ext
        self.cancel_event = cancel_event or threading.Event()
        self.progress = progress
        self.record_empty_dirs = record_empty_dirs
        self.min_images = max(int(min_images), 1)
        self.skip_cover_only = skip_cover_only
        self.cover_with_archives_max = cover_with_archives_max
        self.result = ScanResult(root=self.root)

    # ------------------------------------------------------------------
    def run(self) -> ScanResult:
        if not os.path.isdir(self.root):
            self.result.records.append(ScanRecord(self.root, "오류", "작업 폴더에 접근할 수 없습니다"))
            return self.result
        try:
            self._scan(self.root, siblings=None, is_root=True)
        except ScanCancelled:
            self.result.cancelled = True
        self.result.candidates.sort(key=lambda c: c.path.lower())
        return self.result

    # ------------------------------------------------------------------
    def _rec(self, path: str, status: str, reason: str) -> None:
        self.result.records.append(ScanRecord(path, status, reason))

    def _scan(self, path: str, siblings: dict[str, set[str]] | None, is_root: bool) -> _Subtree:
        if self.cancel_event.is_set():
            raise ScanCancelled()
        self.result.dirs_scanned += 1
        if self.progress:
            self.progress(self.result.dirs_scanned, path)

        try:
            with os.scandir(path) as it:
                entries = list(it)
        except OSError as e:
            self._rec(path, "오류", f"폴더를 읽을 수 없음: {e.strerror or e}")
            return _Subtree(False, True)

        images: list[ImageFile] = []
        others: list[str] = []
        subdirs: list[os.DirEntry] = []
        ignored_subdirs: list[str] = []
        local_uncertain = False
        zero_byte: list[str] = []
        names_lower: set[str] = set()
        archive_stems: set[str] = set()

        for e in entries:
            names_lower.add(e.name.lower())
            try:
                if _is_link(e):
                    self._rec(e.path, "건너뜀", "심볼릭 링크/정션은 따라가지 않음")
                    others.append(e.name)
                    continue
                if e.is_dir(follow_symlinks=False):
                    if rules.is_ignored_dir(e.name):
                        if rules.LEGACY_TEMP_DIR_RE.match(e.name):
                            self.result.temp_artifacts.append(e.path)
                        ignored_subdirs.append(e.name)
                        continue
                    subdirs.append(e)
                elif e.is_file(follow_symlinks=False):
                    stem = rules.archive_stem(e.name)
                    if stem is not None:
                        archive_stems.add(stem)
                    if rules.TEMP_FILE_RE.match(e.name):
                        self.result.temp_artifacts.append(e.path)
                        continue
                    if rules.is_image_name(e.name):
                        st = e.stat(follow_symlinks=False)
                        images.append(ImageFile(e.name, st.st_size, st.st_mtime_ns))
                        if st.st_size == 0:
                            zero_byte.append(e.name)
                    else:
                        others.append(e.name)
                else:
                    others.append(e.name)
            except OSError as ex:
                local_uncertain = True
                self._rec(e.path, "오류", f"파일 정보를 읽을 수 없음: {ex.strerror or ex}")

        # 하위 폴더 먼저 처리 (깊이 우선)
        child_siblings = {"names": names_lower, "stems": archive_stems}
        sub_has_images = False
        sub_uncertain = False
        empty_subdirs: list[str] = []
        for sd in sorted(subdirs, key=lambda d: d.name.lower()):
            info = self._scan(sd.path, child_siblings, is_root=False)
            sub_has_images |= info.has_images
            sub_uncertain |= info.uncertain
            if not info.has_images and not info.uncertain:
                empty_subdirs.append(sd.name)

        if images:
            n = len(images)
            if is_root:
                self._rec(path, "건너뜀", f"작업 루트 폴더 자체는 압축하지 않음 (직접 이미지 {n}개 보존)")
            elif sub_has_images:
                self._rec(path, "건너뜀", f"하위 폴더에 이미지가 있어 상위 폴더 보존 (직접 이미지 {n}개는 그대로 둠)")
            elif sub_uncertain or local_uncertain:
                self._rec(path, "건너뜀", "스캔 중 오류가 있어 압축 대상에서 제외")
            else:
                self._make_candidate(path, images, others, empty_subdirs, zero_byte, siblings)
        else:
            if is_root:
                pass
            elif not subdirs and self.record_empty_dirs:
                self._rec(path, "건너뜀", "이미지 파일 없음")
            elif not subdirs and archive_stems:
                pass  # 압축파일만 있는 폴더(유형 C) - 조용히 통과

        return _Subtree(bool(images) or sub_has_images, local_uncertain or sub_uncertain)

    def _make_candidate(self, path, images, others, empty_subdirs, zero_byte, siblings) -> None:
        parent, name = os.path.split(path.rstrip("\\/"))
        out_name = name + self.output_ext
        out_path = os.path.join(parent, out_name)

        if siblings is not None:
            if out_name.lower() in siblings["names"]:
                self._rec(path, "건너뜀", f"같은 이름의 파일이 이미 있음: {out_name}")
                return
            if name.lower() in siblings["stems"]:
                self._rec(path, "건너뜀", "같은 이름의 압축파일이 이미 있음 (기존 압축파일 유지)")
                return
        if os.path.lexists(out_path):
            self._rec(path, "건너뜀", f"출력 파일이 이미 있음: {out_name}")
            return

        # 표지/낱장 이미지 보호 -------------------------------------------
        n = len(images)
        archives_here = [o for o in others if rules.is_archive_name(o)]
        if self.skip_cover_only and archives_here and n <= self.cover_with_archives_max:
            self._rec(path, "건너뜀",
                      f"압축파일 {len(archives_here)}개와 함께 있는 낱장 이미지 {n}개(표지 등)는 보존: "
                      + ", ".join(i.name for i in images[:3]))
            return
        if self.skip_cover_only and all(rules.is_cover_name(i.name) for i in images):
            self._rec(path, "건너뜀", "표지로 보이는 이미지만 있음 (보존): " + ", ".join(i.name for i in images[:3]))
            return
        if n < self.min_images:
            self._rec(path, "건너뜀", f"이미지가 {n}장뿐이라 제외 (최소 {self.min_images}장)")
            return

        images.sort(key=lambda i: i.name.lower())
        warnings = []
        if zero_byte:
            warnings.append(f"0바이트 이미지 {len(zero_byte)}개 (손상 의심): " + ", ".join(zero_byte[:5]))
        archives_inside = [o for o in others if rules.is_archive_name(o)]
        if archives_inside:
            warnings.append(f"폴더 안 압축파일 {len(archives_inside)}개는 넣지 않음")
        if others:
            warnings.append(f"이미지 외 파일 {len(others)}개는 압축하지 않고 보존")
        if empty_subdirs:
            warnings.append(f"이미지 없는 하위 폴더 {len(empty_subdirs)}개는 보존")

        c = Candidate(
            path=path, parent=parent, name=name, images=images, other_files=others,
            empty_subdirs=empty_subdirs, output_path=out_path, warnings=warnings,
        )
        self.result.candidates.append(c)
        self._rec(path, "대상", "; ".join(warnings) if warnings else "이미지 폴더")


def scan(root: str, **kw) -> ScanResult:
    return Scanner(root, **kw).run()
