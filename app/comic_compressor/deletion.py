"""Delete only files verified through the same Windows handles used for deletion."""
from contextlib import ExitStack
import ctypes as c
from ctypes import wintypes as w
import os
from pathlib import Path
import zipfile


class PartialDeletionError(OSError):
    """Deletion disposition or handle close failed after committing cleanup."""


def delete_verified_originals(candidate, control, progress=lambda *_: None):
    from .core import hash_stream, snapshot
    if os.name != 'nt':
        raise OSError('검증된 원본 삭제는 Windows에서만 지원합니다')
    if len(candidate.files) > 4096:
        raise OSError('4096개를 초과하는 파일은 원본을 자동 삭제하지 않습니다')
    import msvcrt
    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.GetFileInformationByHandleEx.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
    kernel.SetFileInformationByHandle.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]

    def long_path(path):
        value = str(Path(path).absolute())
        if value.startswith('\\\\?\\'):
            return value
        if value.startswith('\\\\'):
            return '\\\\?\\UNC\\' + value[2:]
        return '\\\\?\\' + value

    class AttributeTag(c.Structure):
        _fields_ = [('attributes', w.DWORD), ('tag', w.DWORD)]

    class Disposition(c.Structure):
        _fields_ = [('delete', c.c_ubyte)]

    def open_handle(path, delete=False, directory=False):
        access = 0x80000000 | (0x10000 if delete else 0)
        share = 3 if directory else 1  # No write/delete sharing on files.
        flags = 0x00200000 | (0x02000000 if directory else 0)
        handle = kernel.CreateFileW(long_path(path), access, share, None, 3, flags, None)
        if handle in (None, c.c_void_p(-1).value):
            raise OSError(c.get_last_error(), f'안전한 읽기/삭제 잠금 실패: {path}')
        info = AttributeTag()
        if not kernel.GetFileInformationByHandleEx(handle, 9, c.byref(info), c.sizeof(info)) or info.attributes & 0x400:
            kernel.CloseHandle(handle)
            raise OSError(f'파일 속성 검사 실패 또는 재분석 지점: {path}')
        return handle

    def stream_for(stack, path, delete=False):
        handle = open_handle(path, delete=delete)
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except Exception:
            kernel.CloseHandle(handle)
            raise
        stream = stack.enter_context(os.fdopen(fd, 'rb'))
        return stream, handle

    def disposition(handle, delete):
        info = Disposition(int(delete))
        if not kernel.SetFileInformationByHandle(handle, 4, c.byref(info), c.sizeof(info)):
            raise OSError(c.get_last_error(), '검증된 파일의 삭제 표시 설정 실패')

    def check_folder():
        images, dirs, others = snapshot(candidate.path, control)
        if dirs or others or images != candidate.files:
            raise OSError('이미지 외 파일/하위 폴더가 있거나 원본이 변경되어 삭제하지 않습니다')

    control.check()
    directory_handle = open_handle(candidate.path, directory=True)
    committed = False
    try:
        check_folder()
        with ExitStack() as stack:
            archive_stream, _ = stream_for(stack, candidate.output)
            zipped = stack.enter_context(zipfile.ZipFile(archive_stream))
            expected = {item.name: item for item in candidate.files}
            infos = zipped.infolist()
            if len(infos) != len(expected) or {i.filename for i in infos} != set(expected):
                raise OSError('삭제 전 ZIP 내부 목록이 원본과 일치하지 않습니다')
            locked = []
            for item in candidate.files:
                control.check()
                source, handle = stream_for(stack, candidate.path / item.name, delete=True)
                locked.append((item, source, handle))
            for index, (item, source, handle) in enumerate(locked, 1):
                progress(f'삭제 전 잠금/해시 검증 {index}/{len(locked)}: {item.name}')
                with zipped.open(item.name) as archived:
                    if hash_stream(source, control) != hash_stream(archived, control):
                        raise OSError(f'삭제 전 원본/ZIP SHA-256 불일치: {item.name}')
            check_folder()
            control.check()
            progress('검증된 원본 삭제 중 · 현재 폴더 정리 완료 후 중단 적용')
            marked = []
            try:
                for item, source, handle in locked:
                    disposition(handle, True)
                    marked.append(handle)
            except Exception as exc:
                rollback_errors = []
                for handle in marked:
                    try:
                        disposition(handle, False)
                    except OSError as rollback:
                        rollback_errors.append(str(rollback))
                if rollback_errors:
                    raise PartialDeletionError('일부 검증된 파일의 삭제 표시를 복구하지 못했습니다: ' + '; '.join(rollback_errors)) from exc
                raise
            committed = True
    except Exception as exc:
        if committed:
            raise PartialDeletionError(f'삭제 처리 중 오류가 발생했습니다. 원본 폴더를 확인하세요: {exc}') from exc
        raise
    finally:
        kernel.CloseHandle(directory_handle)
    try:
        candidate.path.rmdir()  # Empty directory only. Never recurse.
    except OSError as exc:
        return '이미지 삭제·폴더 보존', f'검증된 이미지 삭제 완료; 빈 폴더 제거 보류: {exc}'
    return '삭제', '삭제 직전 잠금·목록·SHA-256 재검증 후 원본 폴더 삭제 완료'
