import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image
from comic_compressor.core import Control, Cancelled, compress, scan, discover_bandizip
from comic_compressor.deletion import delete_verified_originals


@unittest.skipUnless(os.name == 'nt', 'Windows verified handle deletion')
class DeletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ncc-delete-test-', dir=os.environ.get('NCC_TEST_TEMP'))
        self.root = Path(self.temp.name)
        self.source = self.root / '원본 日本語'
        self.source.mkdir()
        for index in range(2):
            Image.new('RGB', (16, 20), 'blue').save(self.source / f'{index}.png')
        self.candidate = scan(self.root).candidates[0]

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def runner(args, cwd, control, log):
        if args[1] != 't':
            with zipfile.ZipFile(args[6], 'w') as archive:
                for arg in args[7:]:
                    archive.write(cwd / arg[2:], arg[2:])

    def compress(self, **kwargs):
        return compress(self.candidate, 'fixture', Control(), runner=self.runner, **kwargs)

    def test_default_preserves_originals(self):
        result = self.compress()
        self.assertEqual(result.status, '성공')
        self.assertEqual(result.original_action, '보존')
        self.assertEqual(self.candidate.output.suffix, '.zip')
        self.assertTrue((self.source / '0.png').exists())

    def test_real_bandizip_then_verified_deletion(self):
        bz = discover_bandizip()
        if not bz:
            self.skipTest('Bandizip not installed')
        result = compress(self.candidate, bz, Control(), delete_originals=True)
        self.assertEqual(result.status, '성공', result.detail)
        self.assertEqual(result.original_action, '삭제', result.detail)
        self.assertFalse(self.source.exists())
        self.assertTrue(self.candidate.output.exists())

    def test_opt_in_deletes_verified_image_only_folder(self):
        result = self.compress(delete_originals=True)
        self.assertEqual(result.status, '성공', result.detail)
        self.assertEqual(result.original_action, '삭제', result.detail)
        self.assertFalse(self.source.exists())
        with zipfile.ZipFile(self.candidate.output) as archive:
            self.assertEqual(set(archive.namelist()), {'0.png', '1.png'})
            self.assertIsNone(archive.testzip())

    def test_non_image_file_blocks_deletion(self):
        (self.source / '메모.txt').write_text('keep', encoding='utf-8')
        result = self.compress(delete_originals=True)
        self.assertEqual(result.status, '성공')
        self.assertEqual(result.original_action, '삭제 보류')
        self.assertEqual(len(list(self.source.iterdir())), 3)

    def test_archive_inside_original_blocks_deletion(self):
        (self.source / '기존.zip').write_bytes(b'keep')
        result = self.compress(delete_originals=True)
        self.assertEqual(result.original_action, '삭제 보류')
        self.assertEqual((self.source / '기존.zip').read_bytes(), b'keep')
        self.assertTrue((self.source / '0.png').exists())

    def test_busy_source_blocks_deletion(self):
        self.compress()
        with (self.source / '0.png').open('rb'):
            with self.assertRaises(OSError):
                delete_verified_originals(self.candidate, Control())
        self.assertTrue((self.source / '0.png').exists())
        self.assertTrue((self.source / '1.png').exists())

    def test_busy_archive_blocks_deletion(self):
        self.compress()
        with self.candidate.output.open('r+b'):
            with self.assertRaises(OSError):
                delete_verified_originals(self.candidate, Control())
        self.assertTrue((self.source / '0.png').exists())

    def test_changed_archive_hash_blocks_deletion(self):
        self.compress()
        with zipfile.ZipFile(self.candidate.output, 'w') as archive:
            archive.writestr('0.png', b'changed')
            archive.writestr('1.png', b'changed')
        with self.assertRaisesRegex(OSError, 'SHA-256'):
            delete_verified_originals(self.candidate, Control())
        self.assertTrue((self.source / '0.png').exists())

    def test_wrong_archive_list_blocks_deletion(self):
        self.compress()
        with zipfile.ZipFile(self.candidate.output, 'w') as archive:
            archive.writestr('other.png', b'wrong')
        with self.assertRaisesRegex(OSError, '목록'):
            delete_verified_originals(self.candidate, Control())
        self.assertTrue((self.source / '0.png').exists())

    def test_cancel_during_delete_verification_preserves_all(self):
        self.compress()
        control = Control()
        def cancel(message):
            control.stop()
        with self.assertRaises(Cancelled):
            delete_verified_originals(self.candidate, control, cancel)
        self.assertEqual(len(list(self.source.iterdir())), 2)

    def test_new_non_image_before_delete_commit_preserves_all(self):
        self.compress()
        def add_file(message):
            if '삭제 전' in message:
                (self.source / 'new.txt').write_text('keep', encoding='utf-8')
        with self.assertRaises(OSError):
            delete_verified_originals(self.candidate, Control(), add_file)
        self.assertEqual(len(list(self.source.iterdir())), 3)

    def test_locks_prevent_source_and_archive_writes(self):
        self.compress()
        attempts = []
        def attempt(message):
            if '삭제 전' in message:
                for path in (self.source / '0.png', self.candidate.output):
                    try:
                        with path.open('r+b'):
                            pass
                    except PermissionError:
                        attempts.append(True)
                    else:
                        self.fail(f'write lock did not protect {path}')
        action, _ = delete_verified_originals(self.candidate, Control(), attempt)
        self.assertEqual(action, '삭제')
        self.assertEqual(len(attempts), 4)


if __name__ == '__main__':
    unittest.main()
