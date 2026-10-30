import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image
from comic_compressor.core import compress, scan, Control, Cancelled, retry_access


class NasAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ncc-nas-', dir=os.environ.get('NCC_TEST_TEMP'))
        self.root = Path(self.temp.name)
        self.source = self.root / '원본'
        self.source.mkdir()
        Image.new('RGB', (10, 12), 'red').save(self.source / '001.png')
        self.candidate = scan(self.root).candidates[0]
        self.logs = []
        self.retry_patch = patch('comic_compressor.core.ACCESS_RETRY_DELAYS', (0, 0))
        self.retry_patch.start()

    def tearDown(self):
        self.retry_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def runner(args, cwd, control, log):
        if args[1] != 't':
            with zipfile.ZipFile(args[6], 'w') as archive:
                for arg in args[7:]:
                    archive.write(cwd / arg[2:], arg[2:])

    def run_job(self, **kwargs):
        return compress(self.candidate, 'fixture', Control(), self.logs.append, runner=self.runner, **kwargs)

    def deny_write_open(self):
        original = Path.open
        def open_file(path, mode='r', *args, **kwargs):
            if path.name == 'result.zip' and mode == 'r+b':
                raise PermissionError(13, 'NAS denies reopening archive for writing', str(path))
            return original(path, mode, *args, **kwargs)
        return patch.object(Path, 'open', open_file)

    def test_write_reopen_denied_still_publishes_verified_zip(self):
        with self.deny_write_open():
            result = self.run_job()
        self.assertEqual(result.status, '성공', result.detail)
        self.assertEqual(result.original_action, '보존')
        self.assertIn('동기화 확인 불가', result.detail)
        self.assertTrue(self.candidate.output.exists())
        self.assertTrue((self.source / '001.png').exists())
        with zipfile.ZipFile(self.candidate.output) as archive:
            self.assertEqual(archive.read('001.png'), (self.source / '001.png').read_bytes())

    def test_unsynced_zip_never_calls_opt_in_deletion(self):
        with self.deny_write_open(), patch('comic_compressor.deletion.delete_verified_originals') as deletion:
            result = self.run_job(delete_originals=True)
        deletion.assert_not_called()
        self.assertEqual(result.status, '성공')
        self.assertEqual(result.original_action, '삭제 보류')
        self.assertTrue((self.source / '001.png').exists())

    def test_transient_read_lock_retries_then_succeeds(self):
        original = Path.open
        attempts = []
        def open_file(path, mode='r', *args, **kwargs):
            if path.name == 'result.zip' and mode == 'rb':
                attempts.append(1)
                if len(attempts) < 3:
                    raise PermissionError(13, 'temporary read lock')
            return original(path, mode, *args, **kwargs)
        with patch.object(Path, 'open', open_file):
            result = self.run_job()
        self.assertEqual(result.status, '성공', result.detail)
        self.assertEqual(len(attempts), 3)
        self.assertTrue(any('접근 재시도' in line for line in self.logs))

    def test_permanent_read_denial_fails_at_clear_stage(self):
        original = Path.open
        def open_file(path, mode='r', *args, **kwargs):
            if path.name == 'result.zip' and mode == 'rb':
                raise PermissionError(13, 'permanent read denial')
            return original(path, mode, *args, **kwargs)
        with patch.object(Path, 'open', open_file):
            result = self.run_job()
        self.assertEqual(result.status, '실패')
        self.assertIn('ZIP 내부 목록 및 SHA-256 검증', result.detail)
        self.assertFalse(self.candidate.output.exists())
        self.assertTrue((self.source / '001.png').exists())

    def test_real_fsync_error_is_not_bypassed(self):
        with patch('comic_compressor.core.os.fsync', side_effect=OSError(5, 'disk flush failed')):
            result = self.run_job(delete_originals=True)
        self.assertEqual(result.status, '실패')
        self.assertIn('ZIP 저장 동기화', result.detail)
        self.assertFalse(self.candidate.output.exists())
        self.assertTrue((self.source / '001.png').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows publication semantics')
    def test_transient_rename_lock_retries(self):
        original = os.rename
        attempts = []
        def rename(src, dst, *args, **kwargs):
            if Path(src).name == 'result.zip':
                attempts.append(1)
                if len(attempts) == 1:
                    raise PermissionError(13, 'temporary rename lock')
            return original(src, dst, *args, **kwargs)
        with patch('comic_compressor.core.os.rename', side_effect=rename):
            result = self.run_job()
        self.assertEqual(result.status, '성공', result.detail)
        self.assertEqual(len(attempts), 2)

    @unittest.skipUnless(os.name == 'nt', 'Windows publication semantics')
    def test_permanent_rename_denial_keeps_originals(self):
        with patch('comic_compressor.core.os.rename', side_effect=PermissionError(13, 'rename denied')):
            result = self.run_job(delete_originals=True)
        self.assertEqual(result.status, '실패')
        self.assertIn('최종 ZIP 이름 확정', result.detail)
        self.assertFalse(self.candidate.output.exists())
        self.assertTrue((self.source / '001.png').exists())

    def test_cleanup_denial_does_not_mask_hash_mismatch(self):
        original = Path.unlink
        def unlink(path, *args, **kwargs):
            if path.name == 'result.zip':
                raise PermissionError(13, 'cleanup denied')
            return original(path, *args, **kwargs)
        def wrong_runner(args, cwd, control, log):
            if args[1] != 't':
                with zipfile.ZipFile(args[6], 'w') as archive:
                    archive.writestr('001.png', b'wrong content')
        with patch.object(Path, 'unlink', unlink):
            result = compress(self.candidate, 'fixture', Control(), self.logs.append, runner=wrong_runner)
        self.assertEqual(result.status, '실패')
        self.assertIn('SHA-256 불일치', result.detail)
        self.assertTrue(any('임시 정리 보류' in line for line in self.logs))
        self.assertTrue((self.source / '001.png').exists())

    def test_cancel_during_retry_exits_immediately(self):
        control = Control()
        def fail():
            raise PermissionError(13, 'locked')
        def stop(message):
            control.stop()
        with self.assertRaises(Cancelled):
            retry_access(fail, control, stop, 'test')

    def test_other_errors_are_not_retried(self):
        calls = []
        def fail():
            calls.append(1)
            raise OSError(5, 'disk I/O error')
        with self.assertRaises(OSError):
            retry_access(fail, Control(), self.logs.append, 'test')
        self.assertEqual(len(calls), 1)

    @unittest.skipUnless(os.name == 'nt', 'Windows read-only attribute')
    def test_actual_readonly_archive_can_publish_but_never_delete_source(self):
        def readonly_runner(args, cwd, control, log):
            self.runner(args, cwd, control, log)
            if args[1] != 't':
                os.chmod(args[6], stat.S_IREAD)
        try:
            result = compress(self.candidate, 'fixture', Control(), self.logs.append,
                              runner=readonly_runner, delete_originals=True)
            self.assertEqual(result.status, '성공', result.detail)
            self.assertEqual(result.original_action, '삭제 보류')
            self.assertTrue((self.source / '001.png').exists())
            self.assertTrue(self.candidate.output.exists())
        finally:
            if self.candidate.output.exists():
                os.chmod(self.candidate.output, stat.S_IWRITE | stat.S_IREAD)


if __name__ == '__main__':
    unittest.main()
