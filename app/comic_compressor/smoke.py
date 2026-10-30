"""Opt-in packaged EXE diagnostic. Only creates data inside a new test directory."""
import json
import platform
import sys
import tempfile
import traceback
from pathlib import Path

from PySide6.QtCore import QTimer
from PIL import Image

from .core import scan, compress, Control, discover_bandizip


def smoke_test(app, window, directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    result = {'python': sys.version, 'platform': platform.platform(), 'frozen': getattr(sys, 'frozen', False),
              'default_root': window.root.text(), 'bandizip': discover_bandizip()}
    code = 0
    try:
        with tempfile.TemporaryDirectory(prefix='exe-smoke-', dir=directory) as temp:
            root = Path(temp)
            for relative in ('슬램덩크/01권/001.jpg', '슬램덩크/02권/001.jpg', '日本語 & 공백/001.png',
                             '작품C/cover.jpg', '작품C/01권/001.jpg'):
                file = root / relative
                file.parent.mkdir(parents=True, exist_ok=True)
                Image.new('RGB', (24, 32), '#617fa3').save(file)
            (root / '기존').mkdir()
            (root / '기존/01.zip').write_bytes(b'preserve')
            found = scan(root)
            assert len(found.candidates) == 4, found
            window.root.setText(str(root))
            window.show_scan(found)
            window.set_busy(False)
            window.logs.appendPlainText('진단: 생성된 테스트 이미지로만 검증합니다. 실제 NAS 파일은 사용하지 않습니다.')
            records = [compress(c, result['bandizip'], Control()) for c in found.candidates]
            assert all(r.status == '성공' for r in records), records
            for index, rec in enumerate(records):
                window.show_result(index, rec)
            window.show_progress(4, 4)
            window.current.setText('EXE 진단 완료 · 테스트 ZIP 4개 · 무결성 및 SHA-256 검증 통과')
            result['candidates'] = len(found.candidates)
            result['success'] = len(records)
            result['output_format'] = 'zip'
            assert all(c.output.suffix == '.zip' for c in found.candidates)
            result['originals_preserved'] = all((c.path / f.name).is_file() for c in found.candidates for f in c.files)
            window.show()
            app.processEvents()
            window.grab().save(str(directory / 'gui-preview.png'))
            result['ok'] = True
    except Exception:
        result['ok'] = False
        result['error'] = traceback.format_exc()
        code = 1
    (directory / 'smoke-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    window.close()
    return code
