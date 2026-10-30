"""확장자 및 제외 규칙 (GUI/Qt 비의존)."""
from __future__ import annotations

import os
import re

IMAGE_EXTS = frozenset({
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".avif",
})

ARCHIVE_EXTS = frozenset({
    ".zip", ".cbz", ".rar", ".cbr", ".7z", ".cb7", ".tar", ".cbt",
})

# 탐색하지 않는 폴더 (소문자 비교). '.'으로 시작하는 폴더도 모두 제외한다.
IGNORED_DIR_NAMES = frozenset({
    "@eadir", "#recycle", "#snapshot", "@recycle", "@recently-snapshot",
    "$recycle.bin", "system volume information", "recycler",
    "__macosx", "@tmp", "#tmp",
})

# 이 프로그램이 만드는 임시 압축파일:  .ncc-<token>.tmp.zip
TEMP_FILE_RE = re.compile(r"^\.ncc-[0-9a-z]{8,32}\.tmp\.zip$", re.IGNORECASE)
# 이전(GPT) 버전이 만들던 임시 폴더: .ncc-xxxxxxxx.tmp  (내부에 result.zip)
LEGACY_TEMP_DIR_RE = re.compile(r"^\.ncc-[0-9a-z_]{6,32}\.tmp$", re.IGNORECASE)


def ext_of(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def is_image_name(name: str) -> bool:
    return ext_of(name) in IMAGE_EXTS


def is_archive_name(name: str) -> bool:
    return ext_of(name) in ARCHIVE_EXTS


def archive_stem(name: str) -> str | None:
    """'01권.zip' -> '01권' (소문자). 압축파일이 아니면 None."""
    ext = ext_of(name)
    if ext in ARCHIVE_EXTS:
        return name[: -len(ext)].lower()
    if name.lower().endswith((".tar.gz", ".tar.bz2", ".tar.xz")):
        return name.rsplit(".", 2)[0].lower()
    return None


def is_ignored_dir(name: str) -> bool:
    return name.startswith(".") or name.lower() in IGNORED_DIR_NAMES


# 간단한 이미지 헤더(매직 넘버) 확인 - 디코딩 없이 앞부분 몇 바이트만 비교
_MAGIC = {
    ".jpg": [b"\xff\xd8\xff"],
    ".jpeg": [b"\xff\xd8\xff"],
    ".png": [b"\x89PNG\r\n\x1a\n"],
    ".gif": [b"GIF87a", b"GIF89a"],
    ".bmp": [b"BM"],
    ".tif": [b"II*\x00", b"MM\x00*"],
    ".tiff": [b"II*\x00", b"MM\x00*"],
}


def header_looks_valid(name: str, head: bytes) -> bool:
    ext = ext_of(name)
    if ext == ".webp":
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if ext == ".avif":
        return len(head) >= 12 and head[4:8] == b"ftyp"
    sigs = _MAGIC.get(ext)
    if not sigs:
        return True
    return any(head.startswith(s) for s in sigs)


# 표지로 흔히 쓰이는 파일명 (확장자 제외, 소문자):  cover, cover1, 0000, 000, 00, folder, poster, thumb ...
_COVER_RE = re.compile(
    r"^(?:0+|(?:cover|covers|front|folder|poster|thumb|thumbnail|title|표지)(?:[\s_\-]*\d{1,2})?)$",
    re.IGNORECASE,
)


def is_cover_name(name: str) -> bool:
    stem = os.path.splitext(name)[0].strip()
    return bool(_COVER_RE.match(stem))
