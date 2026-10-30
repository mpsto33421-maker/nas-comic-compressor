import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import Qt

from comic_compressor.core import scan, discover_bandizip
from comic_compressor.gui import MainWindow, Worker, STYLE


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(STYLE)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ncc-ui-', dir=os.environ.get('NCC_TEST_TEMP'))
        self.root = Path(self.temp.name)
        source = self.root / '입력/日本語 만화'
        source.mkdir(parents=True)
        Image.new('RGB', (10, 12), 'red').save(source / '001.png')
        self.input = self.root / '입력'

    def tearDown(self):
        self.app.processEvents()
        self.temp.cleanup()

    def wait_worker(self, worker, deadline=15):
        end = time.monotonic() + deadline
        while worker.isRunning() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.01)
        if worker.isRunning():
            worker.control.stop()
            worker.wait(5000)
            self.fail('worker did not finish within timeout')
        worker.wait()
        self.app.processEvents()

    def test_table_selection_and_path_invalidation(self):
        window = MainWindow()
        window.show_scan(scan(self.input))
        window.set_busy(False)
        self.assertEqual(window.table.rowCount(), 2)
        self.assertTrue(window.start_button.isEnabled())
        window.select_all(False)
        self.assertEqual(window.table.item(0, 0).checkState(), Qt.CheckState.Unchecked)
        window.select_all(True)
        self.assertEqual(window.table.item(0, 0).checkState(), Qt.CheckState.Checked)
        window.root.setText(str(self.root))
        self.assertIsNone(window.found)
        self.assertFalse(window.start_button.isEnabled())
        window.close()

    def test_scan_worker_signals_and_csv(self):
        worker = Worker('scan', root=self.input)
        found, completed, errors = [], [], []
        worker.scanned.connect(found.append)
        worker.completed.connect(completed.append)
        worker.error.connect(errors.append)
        with patch('comic_compressor.core.data_dir', return_value=self.root / 'logs'):
            worker.start()
            self.wait_worker(worker)
        self.assertFalse(errors, errors)
        self.assertEqual(len(found[0].candidates), 1)
        self.assertTrue(Path(completed[0]['csv']).exists())

    def test_real_compress_worker_result_and_audit(self):
        bz = discover_bandizip()
        if not bz:
            self.skipTest('Bandizip not installed')
        found = scan(self.input)
        worker = Worker('compress', candidates=list(enumerate(found.candidates)), bz=bz, scan_result=found)
        completed, errors, progress = [], [], []
        worker.completed.connect(completed.append)
        worker.error.connect(errors.append)
        worker.overall.connect(lambda a, b: progress.append((a, b)))
        with patch('comic_compressor.core.data_dir', return_value=self.root / 'logs'):
            worker.start()
            self.wait_worker(worker)
        self.assertFalse(errors, errors)
        self.assertEqual(completed[0]['results'][0].status, '성공')
        self.assertEqual(progress[-1], (1, 1))
        self.assertIn('성공', Path(completed[0]['csv']).read_text(encoding='utf-8-sig'))

    def test_cancelled_worker_marks_unstarted_originals(self):
        found = scan(self.input)
        worker = Worker('compress', candidates=list(enumerate(found.candidates)), bz='unused', scan_result=found)
        worker.control.stop()
        completed = []
        worker.completed.connect(completed.append)
        with patch('comic_compressor.core.data_dir', return_value=self.root / 'logs'):
            worker.start()
            self.wait_worker(worker)
        self.assertEqual(completed[0]['results'][0].status, '미실행')
        self.assertFalse(found.candidates[0].output.exists())
        self.assertTrue((found.candidates[0].path / '001.png').exists())

    def test_worker_passes_opt_in_deletion(self):
        bz = discover_bandizip()
        if not bz:
            self.skipTest('Bandizip not installed')
        found = scan(self.input)
        worker = Worker('compress', candidates=list(enumerate(found.candidates)), bz=bz,
                        scan_result=found, delete_originals=True)
        completed, errors = [], []
        worker.completed.connect(completed.append)
        worker.error.connect(errors.append)
        with patch('comic_compressor.core.data_dir', return_value=self.root / 'logs'):
            worker.start()
            self.wait_worker(worker)
        self.assertFalse(errors, errors)
        self.assertEqual(completed[0]['results'][0].original_action, '삭제')
        self.assertFalse(found.candidates[0].path.exists())

    def test_cancelled_scan_cannot_start_compression(self):
        from comic_compressor.core import Control
        control = Control()
        control.stop()
        window = MainWindow()
        window.show_scan(scan(self.input, control))
        window.set_busy(False)
        self.assertFalse(window.start_button.isEnabled())
        window.close()

    def test_delete_option_defaults_off_and_decline_keeps_off(self):
        window = MainWindow()
        self.assertFalse(window.delete_option.isChecked())
        with patch.object(QMessageBox, 'warning', return_value=QMessageBox.StandardButton.No):
            window.delete_option.setChecked(True)
        self.assertFalse(window.delete_option.isChecked())
        window.close()

    def test_delete_option_requires_warning_acceptance(self):
        window = MainWindow()
        with patch.object(QMessageBox, 'warning', return_value=QMessageBox.StandardButton.Yes) as warning:
            window.delete_option.setChecked(True)
        warning.assert_called_once()
        self.assertTrue(window.delete_option.isChecked())
        window.delete_option.setChecked(False)
        self.assertIn('보존', window.badge.text())
        window.close()

    def test_existing_zip_is_shown_with_skip_reason_and_no_checkbox(self):
        source = self.input / '日本語 만화'
        source.with_name(source.name + '.zip').write_bytes(b'existing')
        window = MainWindow()
        found = scan(self.input)
        window.show_scan(found)
        window.set_busy(False)
        self.assertFalse(window.start_button.isEnabled())
        self.assertEqual(window.table.rowCount(), 2)
        reasons = [window.table.item(row, 6).text() for row in range(2)]
        self.assertTrue(any('동일 이름 압축파일 존재' in text for text in reasons))
        window.select_all(True)
        for row in range(2):
            self.assertFalse(window.table.item(row, 0).flags() & Qt.ItemFlag.ItemIsUserCheckable)
        self.assertIn('새 압축 대상 없음', window.scan_message())
        self.assertIn('1개', window.scan_message())
        window.close()

    def test_mixed_candidate_and_skipped_rows_select_only_candidates(self):
        other = self.input / 'already'
        other.mkdir()
        Image.new('RGB', (10, 12), 'red').save(other / '001.png')
        (self.input / 'already.zip').write_bytes(b'existing')
        window = MainWindow()
        found = scan(self.input)
        window.show_scan(found)
        window.select_all(False)
        window.select_all(True)
        self.assertEqual(len(found.candidates), 1)
        self.assertEqual(window.table.item(0, 0).checkState(), Qt.CheckState.Checked)
        self.assertTrue(all(not window.table.item(row, 0).flags() & Qt.ItemFlag.ItemIsUserCheckable
                            for row in range(1, window.table.rowCount())))
        window.close()


if __name__ == '__main__':
    unittest.main()
