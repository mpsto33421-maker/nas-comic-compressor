from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QSettings, QUrl
from PySide6.QtGui import QDesktopServices, QColor, QFont
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QFileDialog, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QProgressBar, QPlainTextEdit, QMessageBox,
    QComboBox, QCheckBox, QFrame)

from .core import Control, Audit, Record, scan, compress, discover_bandizip, data_dir
from . import __version__


def fmt_size(value):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if value < 1024:
            return f'{value:,.1f} {unit}'
        value /= 1024
    return f'{value:,.1f} PB'


def default_root():
    return Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(sys.argv[0]).resolve().parent


class Worker(QThread):
    message = Signal(str)
    activity = Signal(str)
    scanned = Signal(object)
    result = Signal(int, object)
    overall = Signal(int, int)
    completed = Signal(object)
    error = Signal(str)

    def __init__(self, mode, root=None, candidates=None, bz='', level=0, verify=True, scan_result=None, delete_originals=False):
        super().__init__()
        self.mode, self.root = mode, root
        self.candidates = candidates or []
        self.bz, self.level, self.verify = bz, level, verify
        self.control = Control()
        self.scan_result = scan_result
        self.delete_originals = delete_originals

    def run(self):
        audit = None
        try:
            audit = Audit()
            self.message.emit(f'CSV: {audit.csv_path}')
            def log(message):
                audit.log(message)
                self.message.emit(message)
            if self.mode == 'scan':
                last = 0.0
                def on_scan(count, rec):
                    nonlocal last
                    audit.record(rec)
                    audit.log(f'{rec.status}: {rec.path}: {rec.detail}')
                    now = time.monotonic()
                    if rec.status == '스캔 오류' or now - last > 0.1:
                        self.activity.emit(f'{count:,}개 폴더 검사 · {rec.path}')
                        self.message.emit(f'{rec.status}: {rec.path} · {rec.detail}')
                        last = now
                found = scan(self.root, self.control, on_scan)
                self.scanned.emit(found)
                self.completed.emit({'mode': 'scan', 'csv': str(audit.csv_path)})
            else:
                results = []
                selected = {str(c.path) for _, c in self.candidates}
                for rec in self.scan_result.records:
                    if rec.status == '대상':
                        audit.record(Record(rec.path, '선택' if rec.path in selected else '건너뜀',
                                            '사용자 선택' if rec.path in selected else '선택 해제',
                                            rec.count, rec.source_bytes, rec.output))
                    else:
                        audit.record(rec)
                for index, (row, candidate) in enumerate(self.candidates):
                    if self.control.cancelled.is_set():
                        rec = Record(str(candidate.path), '미실행', '중단으로 실행하지 않음',
                                     len(candidate.files), candidate.size, str(candidate.output))
                    else:
                        self.activity.emit(f'[{index + 1}/{len(self.candidates)}] {candidate.path.name}')
                        self.result.emit(row, Record(str(candidate.path), '처리 중', ''))
                        rec = compress(candidate, self.bz, self.control, log,
                                       self.activity.emit, self.level, self.verify, delete_originals=self.delete_originals)
                    audit.record(rec)
                    log(f'{rec.status}: {rec.path} · {rec.detail}')
                    results.append(rec)
                    self.result.emit(row, rec)
                    self.overall.emit(index + 1, len(self.candidates))
                self.completed.emit({'mode': 'compress', 'results': results, 'csv': str(audit.csv_path),
                                     'cancelled': self.control.cancelled.is_set()})
        except Exception as exc:
            self.error.emit(f'{type(exc).__name__}: {exc}')
        finally:
            if audit:
                audit.close()


STYLE = '''
QWidget { background:#111822; color:#e5edf7; font-family:"Malgun Gothic"; font-size:12px; }
QMainWindow { background:#111822; }
QLabel#title { font-size:25px; font-weight:700; color:#f5f8ff; }
QLabel#muted { color:#91a3ba; }
QLabel#badge { background:#17362f; color:#7de3ba; padding:7px 12px; border-radius:10px; }
QFrame#panel { background:#192332; border:1px solid #29374a; border-radius:10px; }
QLineEdit, QComboBox { background:#0f1722; border:1px solid #334256; border-radius:6px; padding:8px; }
QPushButton { background:#26354a; border:1px solid #374b66; border-radius:6px; padding:8px 15px; font-weight:600; }
QPushButton:hover { background:#344a66; }
QPushButton:disabled { color:#67768a; background:#1b2635; border-color:#29374a; }
QPushButton#primary { background:#397ef1; color:white; border:1px solid #5597ff; }
QPushButton#primary:hover { background:#5591f6; }
QPushButton#primary:disabled { background:#1b2635; color:#67768a; border-color:#29374a; }
QPushButton#stop { color:#ffb6be; border-color:#784651; }
QTableWidget { background:#151f2d; alternate-background-color:#192535; border:1px solid #2c3c51;
 gridline-color:#253449; selection-background-color:#294973; border-radius:6px; }
QHeaderView::section { background:#213046; color:#b5c6dc; padding:10px; border:0; border-right:1px solid #2d405a; }
QProgressBar { background:#0b1420; border:1px solid #293c54; border-radius:5px; height:20px; text-align:center; }
QProgressBar::chunk { background:#3b82f6; border-radius:4px; }
QPlainTextEdit { background:#0c1420; color:#adc3dc; border:1px solid #293b52; border-radius:6px;
 font-family:"Consolas", "Malgun Gothic"; font-size:11px; padding:6px; }
QCheckBox { spacing:8px; }
QCheckBox::indicator { width:16px; height:16px; }
QToolTip { background:#26364a; color:white; border:1px solid #4a6687; }
'''


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('NAS Comic Compressor ' + __version__)
        self.resize(1280, 900)
        self.setMinimumSize(960, 700)
        self.worker = None
        self.found = None
        self.pending_close = False
        self.settings = QSettings('NASComicCompressor', 'NASComicCompressor')
        body = QWidget()
        self.setCentralWidget(body)
        main = QVBoxLayout(body)
        main.setContentsMargins(24, 20, 24, 20)
        main.setSpacing(13)

        heading = QHBoxLayout()
        titlebox = QVBoxLayout()
        title = QLabel('NAS Comic Compressor')
        title.setObjectName('title')
        titlebox.addWidget(title)
        subtitle = QLabel('이미지 폴더를 찾아 검증된 ZIP으로 · Windows / NAS')
        subtitle.setObjectName('muted')
        titlebox.addWidget(subtitle)
        heading.addLayout(titlebox)
        heading.addStretch()
        self.badge = QLabel('원본 보존 · 기본값')
        self.badge.setObjectName('badge')
        heading.addWidget(self.badge)
        main.addLayout(heading)

        panel = QFrame()
        panel.setObjectName('panel')
        paths = QVBoxLayout(panel)
        paths.setContentsMargins(14, 12, 14, 12)
        self.root = QLineEdit(str(default_root()))
        saved_bz = str(self.settings.value('bz', ''))
        self.bz = QLineEdit(saved_bz if Path(saved_bz).is_file() else discover_bandizip())
        self.root.setPlaceholderText(r'작업 폴더 또는 \\NAS\Comics')
        self.bz.setPlaceholderText('반디집 bz.exe 경로')
        self.folder_button = QPushButton('폴더 선택')
        self.bz_button = QPushButton('경로 변경')
        self.folder_button.clicked.connect(self.choose_root)
        self.bz_button.clicked.connect(self.choose_bz)
        for label, edit, button in [('작업 폴더', self.root, self.folder_button), ('반디집 CLI', self.bz, self.bz_button)]:
            row = QHBoxLayout()
            text = QLabel(label)
            text.setFixedWidth(85)
            row.addWidget(text)
            row.addWidget(edit, 1)
            row.addWidget(button)
            paths.addLayout(row)
        main.addWidget(panel)

        toolbar = QHBoxLayout()
        self.scan_button = QPushButton('압축 대상 검색')
        self.scan_button.setObjectName('primary')
        self.all_button, self.none_button = QPushButton('전체 선택'), QPushButton('전체 해제')
        self.scan_button.clicked.connect(self.start_scan)
        self.all_button.clicked.connect(lambda: self.select_all(True))
        self.none_button.clicked.connect(lambda: self.select_all(False))
        for button in (self.scan_button, self.all_button, self.none_button):
            toolbar.addWidget(button)
        toolbar.addStretch()
        self.level = QComboBox()
        self.level.addItem('저장 방식 · 권장', 0)
        self.level.addItem('빠른 압축', 1)
        toolbar.addWidget(QLabel('압축 방식'))
        toolbar.addWidget(self.level)
        self.verify = QCheckBox('이미지 구조 검사')
        self.verify.setChecked(True)
        self.verify.setToolTip('압축 직전 Pillow verify 실행. 전체 픽셀 디코딩은 하지 않으며 모든 이미지 손상을 검출하지는 못합니다.')
        toolbar.addWidget(self.verify)
        main.addLayout(toolbar)

        self.delete_option = QCheckBox('검증 성공 후 원본 폴더 삭제 (기본: 보존)')
        self.delete_option.setToolTip('이미지 외 파일이 있으면 삭제하지 않습니다. 삭제 직전 ZIP/원본 목록과 SHA-256을 잠금 상태에서 다시 검증합니다.')
        self.delete_option.toggled.connect(self.confirm_deletion)
        main.addWidget(self.delete_option)

        self.stats = QLabel('폴더를 검색하면 이미지가 직접 들어 있는 말단 폴더가 표시됩니다.')
        self.stats.setObjectName('muted')
        main.addWidget(self.stats)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(['선택', '작품 / 폴더', '원본 경로', '이미지 수', '원본 용량', '예상 출력파일', '상태 / 제외 사유'])
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for col, width in enumerate([48, 155, 270, 80, 95, 280, 120]):
            self.table.setColumnWidth(col, width)
        header.setStretchLastSection(True)
        main.addWidget(self.table, 4)

        actions = QHBoxLayout()
        self.start_button = QPushButton('선택 항목 압축 시작')
        self.start_button.setObjectName('primary')
        self.pause_button = QPushButton('일시정지')
        self.pause_button.setToolTip('실행 중인 반디집 명령이 끝나면 안전한 지점에서 대기합니다.')
        self.stop_button = QPushButton('중단')
        self.stop_button.setObjectName('stop')
        self.start_button.clicked.connect(self.start_compress)
        self.pause_button.clicked.connect(self.pause)
        self.stop_button.clicked.connect(self.stop)
        for button in (self.start_button, self.pause_button, self.stop_button):
            actions.addWidget(button)
        actions.addStretch()
        self.log_button = QPushButton('로그 폴더 열기')
        self.log_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(data_dir()))))
        actions.addWidget(self.log_button)
        main.addLayout(actions)
        self.progress = QProgressBar()
        self.progress.setValue(0)
        main.addWidget(self.progress)
        self.current = QLabel('준비됨 · 기존 압축파일과 원본 폴더는 보존됩니다.')
        self.current.setObjectName('muted')
        self.current.setWordWrap(True)
        main.addWidget(self.current)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.document().setMaximumBlockCount(3000)
        main.addWidget(self.logs, 2)
        self.root.textChanged.connect(self.invalidate)
        self.set_busy(False)

    def invalidate(self):
        self.found = None
        self.table.setRowCount(0)
        self.start_button.setEnabled(False)
        self.stats.setText('작업 폴더가 변경되었습니다. 다시 검색하세요.')

    def choose_root(self):
        selected = QFileDialog.getExistingDirectory(self, '작업 폴더 선택', self.root.text())
        if selected:
            self.root.setText(selected)

    def choose_bz(self):
        selected, _ = QFileDialog.getOpenFileName(self, '반디집 bz.exe 선택', self.bz.text(), 'Bandizip CLI (bz.exe)')
        if selected:
            self.bz.setText(selected)

    def select_all(self, checked):
        for row in range(len(self.found.candidates) if self.found else 0):
            self.table.item(row, 0).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def set_busy(self, busy):
        for widget in (self.root, self.bz, self.folder_button, self.bz_button, self.scan_button,
                       self.all_button, self.none_button, self.level, self.verify, self.delete_option, self.table):
            widget.setEnabled(not busy)
        self.start_button.setEnabled(not busy and self.found is not None and bool(self.found.candidates) and not self.found.cancelled)
        self.stop_button.setEnabled(busy)
        self.pause_button.setEnabled(busy)
        if not busy:
            self.pause_button.setText('일시정지')

    def launch(self, worker):
        self.worker = worker
        self.set_busy(True)
        worker.message.connect(self.logs.appendPlainText)
        worker.activity.connect(self.current.setText)
        worker.scanned.connect(self.show_scan)
        worker.result.connect(self.show_result)
        worker.overall.connect(self.show_progress)
        worker.completed.connect(self.done)
        worker.error.connect(self.failure)
        worker.finished.connect(self.thread_finished)
        worker.start()

    def start_scan(self):
        if not self.root.text().strip():
            QMessageBox.warning(self, '경로 필요', '작업 폴더를 지정하세요.')
            return
        self.found = None
        self.table.setRowCount(0)
        self.progress.setRange(0, 0)
        self.logs.appendPlainText('── 메타데이터 기반 폴더 검색 시작 ──')
        self.launch(Worker('scan', root=Path(self.root.text().strip())))

    def show_scan(self, found):
        self.found = found
        excluded = [rec for rec in found.records if rec.status != '대상']
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(found.candidates) + len(excluded))
        for row, candidate in enumerate(found.candidates):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(row, 0, check)
            values = [candidate.path.name, str(candidate.path), f'{len(candidate.files):,}',
                      fmt_size(candidate.size), str(candidate.output), '대기']
            for col, value in enumerate(values, 1):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(row, col, item)
        for row, rec in enumerate(excluded, len(found.candidates)):
            empty = QTableWidgetItem('—')
            empty.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, 0, empty)
            values = [Path(rec.path).name, rec.path, str(rec.count) if rec.count else '—',
                      fmt_size(rec.source_bytes) if rec.source_bytes else '—', rec.output or '—',
                      rec.status + ': ' + rec.detail]
            for col, value in enumerate(values, 1):
                item = QTableWidgetItem(value)
                item.setToolTip(rec.detail if col == 6 else value)
                item.setForeground(QColor('#ffb2bc' if rec.status == '스캔 오류' else '#91a3ba'))
                self.table.setItem(row, col, item)
        self.table.setUpdatesEnabled(True)
        skipped = sum(r.status == '건너뜀' for r in found.records)
        errors = sum(r.status == '스캔 오류' for r in found.records)
        self.stats.setText(f'검사 {found.inspected:,}개  ·  대상 {len(found.candidates):,}개  ·  건너뜀 {skipped:,}개  ·  스캔 오류 {errors:,}개'
                           + ('  ·  검색 중단됨: 재검색 필요' if found.cancelled else ''))

    def scan_message(self):
        if self.found.cancelled:
            return '검색 중단됨: 다시 검색하세요.'
        if self.found.candidates:
            return '검색 완료 · 체크된 항목만 압축합니다. 회색 행에는 제외 사유가 표시됩니다.'
        conflicts = sum(r.detail.startswith('동일 이름 압축파일 존재:') for r in self.found.records)
        return (f'새 압축 대상 없음 · 동일 이름 압축파일이 있는 폴더 {conflicts:,}개 · '
                '표의 제외 사유를 확인하세요. 기존 ZIP이 있는 폴더는 원본 삭제도 실행하지 않습니다.')

    def start_compress(self):
        if not self.found or self.found.cancelled:
            return
        bz = Path(self.bz.text().strip().strip('"'))
        if bz.name.lower() != 'bz.exe' or not bz.is_file():
            QMessageBox.warning(self, '반디집 경로 확인', '설치된 반디집의 bz.exe를 선택하세요.')
            return
        chosen = [(row, c) for row, c in enumerate(self.found.candidates)
                  if self.table.item(row, 0).checkState() == Qt.CheckState.Checked]
        if not chosen:
            QMessageBox.information(self, '선택 필요', '압축할 폴더를 선택하세요.')
            return
        self.settings.setValue('bz', str(bz))
        self.progress.setRange(0, len(chosen))
        self.progress.setValue(0)
        deleting = self.delete_option.isChecked()
        self.logs.appendPlainText('── 순차 ZIP 압축 시작 · SHA-256 검증 · ' + ('검증 후 원본 삭제' if deleting else '원본 보존') + ' ──')
        self.launch(Worker('compress', candidates=chosen, bz=str(bz), level=self.level.currentData(),
                           verify=self.verify.isChecked(), scan_result=self.found, delete_originals=deleting))

    def confirm_deletion(self, enabled):
        if enabled:
            answer = QMessageBox.warning(self, '원본 삭제 옵션',
                '선택한 폴더의 압축·무결성·목록·해시 검증이 모두 성공하면 원본 이미지를 영구 삭제합니다.\n'
                '휴지통으로 이동하지 않습니다. 이미지 외 파일이 있거나 삭제 검증에 실패하면 보존합니다.\n'
                '작업 중에는 다른 프로그램으로 대상 폴더를 수정하지 마세요.\n\n원본 삭제 옵션을 활성화할까요?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                self.delete_option.setChecked(False)
                return
        self.badge.setText('검증 후 원본 삭제' if enabled else '원본 보존 · 기본값')
        self.current.setText('원본 삭제 옵션 활성화됨 · 압축 시작 버튼을 눌러야 실행합니다.' if enabled else '원본 보존 모드')

    def show_result(self, row, rec):
        item = self.table.item(row, 6)
        item.setText(rec.status + (' · ' + rec.original_action if rec.status == '성공' else ''))
        item.setToolTip(rec.detail + ' · 원본 처리: ' + rec.original_action)
        item.setForeground(QColor('#79e1b2' if rec.status == '성공' else '#ffb2bc' if rec.status == '실패' else '#a9c5f5'))
        if rec.status == '성공':
            self.table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)

    def show_progress(self, value, total):
        self.progress.setRange(0, total)
        self.progress.setValue(value)
        self.progress.setFormat('%v / %m 폴더 (%p%)')

    def pause(self):
        if not self.worker:
            return
        if self.worker.control.paused.is_set():
            self.worker.control.paused.clear()
            self.pause_button.setText('일시정지')
            self.logs.appendPlainText('작업 재개')
        else:
            self.worker.control.paused.set()
            self.pause_button.setText('계속')
            self.current.setText('일시정지 요청됨 · 실행 중인 반디집 명령 완료 후 안전한 지점에서 대기')
            self.logs.appendPlainText('일시정지 요청 (반디집 명령은 완료 후 대기)')

    def stop(self):
        if self.worker:
            self.worker.control.stop()
            self.current.setText('중단 요청됨 · 반디집 종료 및 임시파일 정리 중')
            self.stop_button.setEnabled(False)

    def done(self, payload):
        if payload['mode'] == 'scan':
            self.progress.setRange(0, 100)
            self.progress.setValue(0 if self.found.cancelled else 100)
            self.progress.setFormat('%p%')
            message = self.scan_message()
            self.current.setText(message)
            self.logs.appendPlainText(message)
        else:
            records = payload['results']
            success = sum(r.status == '성공' for r in records)
            failed = sum(r.status == '실패' for r in records)
            skipped = sum(r.status == '건너뜀' for r in self.found.records)
            skipped += len(self.found.candidates) - len(records) + sum(r.status == '건너뜀' for r in records)
            cancelled = sum(r.status in ('취소', '미실행') for r in records)
            scan_errors = sum(r.status == '스캔 오류' for r in self.found.records)
            source = sum(r.source_bytes for r in records)
            output = sum(r.output_bytes for r in records)
            removed = sum(r.original_action == '삭제' for r in records)
            text = (f'검사 {self.found.inspected:,} · 대상 {len(self.found.candidates):,} · 선택 {len(records):,} · 성공 {success:,} · 실패 {failed:,} · '
                    f'건너뜀 {skipped:,} · 스캔 오류 {scan_errors:,} · 취소/미실행 {cancelled:,} · 생성 ZIP {success:,}\n'
                    f'전체 대상 원본 {fmt_size(sum(c.size for c in self.found.candidates))} · '
                    f'선택 원본 {fmt_size(source)} → 결과 ZIP {fmt_size(output)} · 원본 폴더 삭제 {removed:,}개')
            self.stats.setText(text)
            self.current.setText('중단 처리 완료' if payload['cancelled'] else '작업 완료')
            self.logs.appendPlainText(text)
        self.logs.appendPlainText(f'CSV 저장 완료: {payload["csv"]}')

    def failure(self, error):
        self.progress.setRange(0, 100)
        self.current.setText('작업 오류: ' + error)
        self.logs.appendPlainText('오류: ' + error)

    def thread_finished(self):
        worker, self.worker = self.worker, None
        self.set_busy(False)
        if worker:
            worker.deleteLater()
        if self.pending_close:
            self.close()

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            answer = QMessageBox.question(self, '작업 중 종료', '작업을 중단하고 임시파일을 정리한 뒤 종료할까요?',
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                          QMessageBox.StandardButton.No)
            if answer == QMessageBox.StandardButton.Yes:
                self.pending_close = True
                self.stop()
            event.ignore()
        else:
            event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    app.setStyleSheet(STYLE)
    window = MainWindow()
    if '--smoke-test' in sys.argv:
        from .smoke import smoke_test
        return smoke_test(app, window, Path(sys.argv[sys.argv.index('--smoke-test') + 1]))
    window.show()
    return app.exec()
