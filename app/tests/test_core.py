from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from comic_compressor.core import (Control, Cancelled, Record, Audit, scan, compress,
    discover_bandizip, run_bz, batches)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ncc-test-', dir=os.environ.get('NCC_TEST_TEMP'))
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def image(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (16, 20), '#627faa').save(path)
        return path

    def found(self):
        return scan(self.root).candidates

    def test_01_direct_images(self):
        self.image('작품/001.jpg')
        self.image('작품/002.PNG')
        found = self.found()
        self.assertEqual(len(found), 1)
        self.assertEqual(len(found[0].files), 2)
        self.assertEqual(found[0].output, self.root / '작품.zip')

    def test_02_volumes(self):
        self.image('작품/01권/001.jpg')
        self.image('작품/02권/001.jpg')
        self.assertEqual({c.path.name for c in self.found()}, {'01권', '02권'})

    def test_03_archives_only(self):
        path = self.root / '작품'
        path.mkdir()
        for ext in ('.zip', '.cbz', '.rar', '.cbr', '.7z', '.tar'):
            (path / ('01' + ext)).write_bytes(b'unchanged')
        self.assertEqual(self.found(), [])
        self.assertTrue(all(p.read_bytes() == b'unchanged' for p in path.iterdir()))

    def test_04_mixed(self):
        self.image('작품/02권/001.jpg')
        (self.root / '작품/01권.zip').write_bytes(b'archive')
        self.assertEqual([c.path.name for c in self.found()], ['02권'])

    def test_05_deep(self):
        self.image('작가별/작가A/작품B/01권/001.jpg')
        self.assertEqual([c.path.name for c in self.found()], ['01권'])

    def test_06_cover_preserved(self):
        cover = self.image('작품C/cover.jpg')
        before = cover.read_bytes()
        self.image('작품C/01권/001.jpg')
        self.image('작품C/02권/001.jpg')
        self.assertEqual({c.path.name for c in self.found()}, {'01권', '02권'})
        self.assertEqual(cover.read_bytes(), before)

    def test_07_08_unicode_special_real_bandizip(self):
        bz = discover_bandizip()
        if not bz:
            self.skipTest('Bandizip not installed')
        self.image('한글 日本語 & 공백 (특수)!/001 日本語.JPG')
        self.image('한글 日本語 & 공백 (특수)!/-옵션.png')
        self.image('한글 日本語 & 공백 (특수)!/@목록.png')
        parent = self.root / '한글 日本語 & 공백 (특수)!'
        (parent / '보존.txt').write_text('메모', encoding='utf-8')
        (parent / 'nested.zip').write_bytes(b'not included')
        candidate = self.found()[0]
        result = compress(candidate, bz, Control())
        self.assertEqual(result.status, '성공', result.detail)
        self.assertTrue((parent / '보존.txt').exists())
        self.assertTrue((parent / 'nested.zip').exists())
        with zipfile.ZipFile(candidate.output) as zipped:
            self.assertEqual(set(zipped.namelist()), {f.name for f in candidate.files})
            for item in zipped.infolist():
                self.assertEqual(item.compress_type, zipfile.ZIP_STORED)
                self.assertEqual(zipped.read(item), (parent / item.filename).read_bytes())
        self.assertFalse(list(self.root.glob('.ncc-*')))

    def test_09_existing_cbz_case_insensitive(self):
        self.image('Book/001.jpg')
        existing = self.root / 'BOOK.CBZ'
        existing.write_bytes(b'keep')
        self.assertEqual(self.found(), [])
        self.assertEqual(existing.read_bytes(), b'keep')

    def test_real_bandizip_multiple_command_batches(self):
        bz = discover_bandizip()
        if not bz:
            self.skipTest('Bandizip not installed')
        for index in range(7):
            self.image(f'묶음/{index:04d}_日本語.png')
        candidate = self.found()[0]
        original_batches = batches
        with patch('comic_compressor.core.batches', side_effect=lambda names: original_batches(names, budget=50)):
            result = compress(candidate, bz, Control(), level=1)
        self.assertEqual(result.status, '성공', result.detail)
        with zipfile.ZipFile(candidate.output) as archive:
            self.assertEqual(len(archive.namelist()), 7)

    @unittest.skipUnless(os.name == 'nt', 'Windows Job lifecycle')
    def test_job_kills_child_on_parent_crash(self):
        import ctypes
        from ctypes import wintypes
        marker = self.root / 'child.pid'
        child_code = f'import os,time,pathlib; pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(30)'
        parent_code = ('import sys; from pathlib import Path; from comic_compressor.core import run_bz,Control; '
                       f'run_bz([sys.executable,"-c",{child_code!r}],Path({str(self.root)!r}),Control(),lambda *a:None)')
        parent = subprocess.Popen([sys.executable, '-c', parent_code], creationflags=subprocess.CREATE_NO_WINDOW)
        handle = None
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists(), 'child failed to start')
            handle = kernel.OpenProcess(0x100000, False, int(marker.read_text()))
            self.assertTrue(handle)
            time.sleep(0.1)
            parent.kill()
            parent.wait(timeout=5)
            self.assertEqual(kernel.WaitForSingleObject(handle, 3000), 0, 'child survived parent crash')
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait()
            if handle:
                kernel.CloseHandle(handle)

    def test_same_name_zip_excluded(self):
        self.image('Book/001.jpg')
        (self.root / 'Book.zip').write_bytes(b'keep')
        self.assertEqual(self.found(), [])

    def test_10_compression_failure_cleans_temp(self):
        original = self.image('Book/001.jpg')
        before = original.read_bytes()
        candidate = self.found()[0]
        def failure(args, cwd, control, log):
            Path(args[6]).write_bytes(b'partial')
            raise RuntimeError('injected compression failure')
        result = compress(candidate, 'fake', Control(), runner=failure)
        self.assertEqual(result.status, '실패')
        self.assertFalse(candidate.output.exists())
        self.assertEqual(original.read_bytes(), before)
        self.assertFalse(list(self.root.glob('.ncc-*')))

    def test_11_cancel_during_compression(self):
        original = self.image('Book/001.jpg')
        candidate = self.found()[0]
        control = Control()
        def cancel(args, cwd, control, log):
            Path(args[6]).write_bytes(b'partial')
            control.stop()
            control.check()
        result = compress(candidate, 'fake', control, runner=cancel)
        self.assertEqual(result.status, '취소')
        self.assertTrue(original.exists())
        self.assertFalse(candidate.output.exists())
        self.assertFalse(list(self.root.glob('.ncc-*')))

    def test_12_permission_denied_scan(self):
        self.image('denied/001.jpg')
        self.image('good/001.jpg')
        original_scandir = os.scandir
        def denied(path):
            if Path(path).name == 'denied':
                raise PermissionError('injected access denied')
            return original_scandir(path)
        with patch('comic_compressor.core.os.scandir', side_effect=denied):
            result = scan(self.root)
        self.assertEqual([c.path.name for c in result.candidates], ['good'])
        self.assertTrue(any(r.status == '스캔 오류' and 'denied' in r.path for r in result.records))

    @unittest.skipUnless(os.name == 'nt', 'Windows ACL test')
    def test_12b_real_windows_acl(self):
        self.image('denied/001.jpg')
        path = self.root / 'denied'
        sid = subprocess.check_output(['whoami', '/user', '/fo', 'csv', '/nh'], text=True).strip().split(',')[-1].strip('"')
        deny = subprocess.run(['icacls', str(path), '/deny', f'*{sid}:(RD)'], capture_output=True)
        self.assertEqual(deny.returncode, 0, deny.stdout + deny.stderr)
        try:
            result = scan(self.root)
            self.assertFalse(result.candidates)
            self.assertTrue(any(r.status == '스캔 오류' for r in result.records), result)
        finally:
            restored = subprocess.run(['icacls', str(path), '/remove:d', f'*{sid}'], capture_output=True)
            self.assertEqual(restored.returncode, 0)

    def test_scan_partial_metadata_failure_excludes_entire_folder(self):
        self.image('bad/001.jpg')
        with patch('comic_compressor.core.snapshot', side_effect=PermissionError('metadata inaccessible')):
            result = scan(self.root)
        self.assertFalse(result.candidates)
        self.assertEqual(result.records[0].status, '스캔 오류')

    def test_scan_cancel_not_silent_success(self):
        self.image('Book/001.jpg')
        control = Control()
        control.stop()
        result = scan(self.root, control)
        self.assertTrue(result.cancelled)
        self.assertFalse(result.candidates)

    def test_changed_source_rejected(self):
        image = self.image('Book/001.jpg')
        candidate = self.found()[0]
        image.write_bytes(image.read_bytes() + b'changed')
        result = compress(candidate, 'fake', Control())
        self.assertEqual(result.status, '실패')
        self.assertFalse(candidate.output.exists())

    def test_new_subfolder_rejected(self):
        self.image('Book/001.jpg')
        candidate = self.found()[0]
        (candidate.path / 'new').mkdir()
        result = compress(candidate, 'fake', Control())
        self.assertEqual(result.status, '실패')
        self.assertFalse(candidate.output.exists())

    def test_damaged_image_preserved(self):
        bad = self.root / 'Book/001.jpg'
        bad.parent.mkdir()
        bad.write_bytes(b'broken image')
        candidate = self.found()[0]
        result = compress(candidate, 'fake', Control())
        self.assertEqual(result.status, '실패')
        self.assertIn('이미지 구조 검사 실패', result.detail)
        self.assertEqual(bad.read_bytes(), b'broken image')

    def fake_zip_runner(self, mutate=None, test_error=False):
        def runner(args, cwd, control, log):
            if args[1] == 't':
                if test_error:
                    raise RuntimeError('integrity failure')
                if mutate:
                    mutate(cwd)
                return
            archive = Path(args[6])
            with zipfile.ZipFile(archive, 'w') as zipped:
                for arg in args[7:]:
                    name = arg[2:]
                    zipped.write(cwd / name, name)
        return runner

    def test_integrity_failure_no_zip(self):
        self.image('Book/001.jpg')
        candidate = self.found()[0]
        result = compress(candidate, 'fake', Control(), runner=self.fake_zip_runner(test_error=True))
        self.assertEqual(result.status, '실패')
        self.assertFalse(candidate.output.exists())

    def test_network_error_preserves_original_and_cleans_temp(self):
        original = self.image('Book/001.jpg')
        before = original.read_bytes()
        candidate = self.found()[0]
        def disconnected(*args):
            raise OSError(53, 'injected network path unavailable')
        result = compress(candidate, 'fake', Control(), runner=disconnected)
        self.assertEqual(result.status, '실패')
        self.assertEqual(original.read_bytes(), before)
        self.assertFalse(candidate.output.exists())
        self.assertFalse(list(self.root.glob('.ncc-*')))

    def test_existing_output_after_scan_is_skipped(self):
        self.image('Book/001.jpg')
        candidate = self.found()[0]
        candidate.output.write_bytes(b'existing archive')
        result = compress(candidate, 'fake', Control())
        self.assertEqual(result.status, '건너뜀')
        self.assertEqual(candidate.output.read_bytes(), b'existing archive')

    def test_unreadable_source_is_not_published(self):
        original = self.image('Book/001.jpg')
        before = original.read_bytes()
        candidate = self.found()[0]
        original_open = Path.open
        def deny(path, *args, **kwargs):
            if path == original:
                raise PermissionError('injected image access denied')
            return original_open(path, *args, **kwargs)
        with patch.object(Path, 'open', deny):
            result = compress(candidate, 'fake', Control(), verify_images=False)
        self.assertEqual(result.status, '실패')
        self.assertEqual(original.read_bytes(), before)
        self.assertFalse(candidate.output.exists())

    def test_archive_hash_mismatch_rejected(self):
        self.image('Book/001.jpg')
        candidate = self.found()[0]
        def runner(args, cwd, control, log):
            if args[1] != 't':
                with zipfile.ZipFile(args[6], 'w') as zipped:
                    zipped.writestr('001.jpg', b'wrong content')
        result = compress(candidate, 'fake', Control(), runner=runner)
        self.assertEqual(result.status, '실패')
        self.assertIn('SHA-256', result.detail)
        self.assertFalse(candidate.output.exists())

    def test_source_same_metadata_mutation_detected(self):
        image = self.image('Book/001.jpg')
        candidate = self.found()[0]
        def mutate(cwd):
            st = image.stat()
            content = image.read_bytes()
            image.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
            os.utime(image, ns=(st.st_atime_ns, st.st_mtime_ns))
        result = compress(candidate, 'fake', Control(), runner=self.fake_zip_runner(mutate=mutate))
        self.assertEqual(result.status, '실패')
        self.assertIn('원본 내용 변경', result.detail)

    def test_output_created_during_job_not_overwritten(self):
        self.image('Book/001.jpg')
        candidate = self.found()[0]
        def race(cwd):
            candidate.output.write_bytes(b'other process result')
        result = compress(candidate, 'fake', Control(), runner=self.fake_zip_runner(mutate=race))
        self.assertEqual(result.status, '실패')
        self.assertEqual(candidate.output.read_bytes(), b'other process result')

    def test_wrong_archive_paths_rejected(self):
        self.image('Book/001.jpg')
        candidate = self.found()[0]
        def runner(args, cwd, control, log):
            if args[1] != 't':
                with zipfile.ZipFile(args[6], 'w') as zipped:
                    zipped.write(cwd / '001.jpg', 'Book/001.jpg')
        result = compress(candidate, 'fake', Control(), runner=runner)
        self.assertEqual(result.status, '실패')
        self.assertIn('파일 목록', result.detail)

    def test_real_child_process_cancelled_promptly(self):
        control = Control()
        timer = threading.Timer(0.4, control.stop)
        timer.start()
        start = time.monotonic()
        try:
            with self.assertRaises(Cancelled):
                run_bz([sys.executable, '-c', 'import time; time.sleep(30)'], self.root, control, lambda *_: None)
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - start, 5)

    def test_nonzero_process_is_failure(self):
        with self.assertRaisesRegex(RuntimeError, '종료 코드 7'):
            run_bz([sys.executable, '-c', 'raise SystemExit(7)'], self.root, Control(), lambda *_: None)

    def test_pause_resume_and_cancel(self):
        control = Control()
        control.paused.set()
        timer = threading.Timer(0.2, control.paused.clear)
        timer.start()
        start = time.monotonic()
        control.check()
        self.assertGreaterEqual(time.monotonic() - start, 0.1)
        timer.join()
        control.paused.set()
        control.stop()
        with self.assertRaises(Cancelled):
            control.check()

    def test_command_batches_cover_every_file(self):
        names = [f'{i:06d}_한글 공백.jpg' for i in range(10000)]
        groups = list(batches(names))
        self.assertGreater(len(groups), 1)
        self.assertEqual([arg[2:] for group in groups for arg in group], names)
        self.assertTrue(all(len(subprocess.list2cmdline(g).encode('utf-16-le')) // 2 <= 20000 for g in groups))

    def test_csv_utf8_and_formula_escape(self):
        audit = Audit(self.root / 'logs')
        audit.record(Record('한글 日本語', '성공', '=formula', 2, 123, 'out.zip', 100))
        audit.log('기록')
        path = audit.csv_path
        audit.close()
        text = path.read_text(encoding='utf-8-sig')
        self.assertIn('한글 日本語', text)
        self.assertIn("'=formula", text)

    def test_many_sibling_folders(self):
        for index in range(300):
            self.image(f'시리즈/{index:04d}/001.jpg')
        result = scan(self.root)
        self.assertEqual(len(result.candidates), 300)
        self.assertEqual(result.inspected, 302)


if __name__ == '__main__':
    unittest.main()
