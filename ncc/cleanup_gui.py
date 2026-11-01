"""'남은 원본 정리' 대화상자."""
from __future__ import annotations

import os
import threading

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QHBoxLayout, QHeaderView,
                               QLabel, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
                               QSplitter, QTableView, QVBoxLayout)

from .cleanup import LeftoverCandidate, LeftoverScan, find_leftovers, verify_and_delete
from .runlog import RunLog, human_size

COLORS = {"삭제됨": "#5fd38d", "검증 통과": "#5fd38d", "보존": "#f0c674", "진행 중": "#6cb6ff",
          "대기": "#d0d0d0", "중단": "#bbbbbb"}


class LeftoverModel(QAbstractTableModel):
    HEAD = ["", "남은 원본 폴더", "압축파일", "이미지 수", "용량", "상태"]

    def __init__(self):
        super().__init__()
        self.items: list[LeftoverCandidate] = []
        self.editable = True

    def set_items(self, items):
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def rowCount(self, p=QModelIndex()):
        return 0 if p.isValid() else len(self.items)

    def columnCount(self, p=QModelIndex()):
        return len(self.HEAD)

    def headerData(self, s, o, role=Qt.DisplayRole):
        return self.HEAD[s] if o == Qt.Horizontal and role == Qt.DisplayRole else None

    def data(self, idx, role=Qt.DisplayRole):
        c = self.items[idx.row()]
        col = idx.column()
        if role == Qt.CheckStateRole and col == 0:
            return Qt.Checked if c.checked else Qt.Unchecked
        if role == Qt.DisplayRole:
            return [None, c.folder, os.path.basename(c.archive), len(c.images),
                    human_size(c.total_bytes), c.status][col]
        if role == Qt.ToolTipRole:
            return c.message or c.folder
        if role == Qt.ForegroundRole and col == 5:
            return QColor(COLORS.get(c.status, "#d0d0d0"))
        return None

    def flags(self, idx):
        f = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if idx.column() == 0 and self.editable:
            f |= Qt.ItemIsUserCheckable
        return f

    def setData(self, idx, value, role=Qt.EditRole):
        if role == Qt.CheckStateRole and idx.column() == 0 and self.editable:
            self.items[idx.row()].checked = Qt.CheckState(value) == Qt.Checked
            self.dataChanged.emit(idx, idx)
            return True
        return False

    def refresh(self, row):
        self.dataChanged.emit(self.index(row, 0), self.index(row, self.columnCount() - 1))


class _ScanThread(QThread):
    progress = Signal(int, str)
    done = Signal(object)

    def __init__(self, root):
        super().__init__()
        self.root = root
        self.cancel = threading.Event()

    def run(self):
        self.done.emit(find_leftovers(self.root, self.cancel, lambda n, p: self.progress.emit(n, p)))


class _DeleteThread(QThread):
    item = Signal(int, bool, str)
    started_item = Signal(int)
    log = Signal(str)
    done = Signal(int, int)

    def __init__(self, items, rows, dry_run):
        super().__init__()
        self.items, self.rows, self.dry = items, rows, dry_run
        self.cancel = threading.Event()

    def run(self):
        log = RunLog(kind="cleanup")
        ok_n = keep_n = 0
        self.log.emit(f"정리 기록: {log.csv_path}")
        try:
            for r in self.rows:
                if self.cancel.is_set():
                    break
                c = self.items[r]
                self.started_item.emit(r)
                ok, msg = verify_and_delete(c, self.cancel, dry_run=self.dry)
                if ok:
                    ok_n += 1
                else:
                    keep_n += 1
                log.row("정리", c.folder, "삭제" if ok and not self.dry else ("검증 통과" if ok else "보존"),
                        msg, len(c.images), c.total_bytes, c.archive, 0, "삭제" if ok and not self.dry else "보존")
                log.line(f"{'✔' if ok else '→'} {c.folder}: {msg}")
                self.item.emit(r, ok, msg)
        finally:
            log.close()
        self.done.emit(ok_n, keep_n)


class CleanupDialog(QDialog):
    def __init__(self, parent, root: str, busy_check=lambda: False):
        super().__init__(parent)
        self.setWindowTitle("남은 원본 정리")
        self.resize(1100, 680)
        self.setMinimumSize(160, 120)
        self.root = root
        self.busy_check = busy_check
        self.scan_th: _ScanThread | None = None
        self.del_th: _DeleteThread | None = None

        v = QVBoxLayout(self)
        info = QLabel(
            "이미 같은 이름의 ZIP/CBZ가 있는데 원본 이미지 폴더가 남아 있는 곳을 찾아 정리합니다.\n"
            "압축파일 안의 파일과 원본을 하나하나 SHA-256으로 비교해서 <b>모든 이미지가 똑같이 들어 있을 때만</b> 삭제하고, "
            "하나라도 없거나 다르면 그 폴더는 그대로 둡니다. 이미지 외 파일·하위 폴더가 있는 폴더도 건드리지 않습니다.")
        info.setTextFormat(Qt.RichText)
        info.setWordWrap(True)
        v.addWidget(info)
        self.lbl_root = QLabel(f"작업 폴더: {root}")
        v.addWidget(self.lbl_root)

        h = QHBoxLayout()
        self.btn_scan = QPushButton("🔍 남은 원본 검색")
        self.btn_scan.setObjectName("primary")
        self.btn_scan.clicked.connect(self.scan)
        h.addWidget(self.btn_scan)
        self.btn_all = QPushButton("전체 선택")
        self.btn_all.clicked.connect(lambda: self._set_all(True))
        self.btn_none = QPushButton("전체 해제")
        self.btn_none.clicked.connect(lambda: self._set_all(False))
        h.addWidget(self.btn_all)
        h.addWidget(self.btn_none)
        self.chk_dry = QCheckBox("시험 실행 (검증만 하고 삭제하지 않음)")
        h.addWidget(self.chk_dry)
        h.addStretch(1)
        self.btn_del = QPushButton("🧹 검증 후 삭제")
        self.btn_del.setObjectName("danger")
        self.btn_del.clicked.connect(self.delete)
        self.btn_stop = QPushButton("⏹ 중단")
        self.btn_stop.clicked.connect(self.stop)
        h.addWidget(self.btn_del)
        h.addWidget(self.btn_stop)
        v.addLayout(h)

        self.model = LeftoverModel()
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        hh = self.table.horizontalHeader()
        for i, w in enumerate([30, 520, 220, 80, 90, 150]):
            self.table.setColumnWidth(i, w)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.doubleClicked.connect(self._open)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(20000)
        sp = QSplitter(Qt.Vertical)
        sp.addWidget(self.table)
        sp.addWidget(self.log)
        sp.setSizes([420, 160])
        v.addWidget(sp, 1)
        self.pb = QProgressBar()
        self.pb.setFormat("%v / %m")
        v.addWidget(self.pb)
        self.lbl = QLabel("[남은 원본 검색]을 누르세요")
        v.addWidget(self.lbl)
        self._update()

    # ------------------------------------------------------------------
    def _running(self):
        return bool((self.scan_th and self.scan_th.isRunning()) or (self.del_th and self.del_th.isRunning()))

    def _update(self):
        run = self._running()
        has = bool(self.model.items)
        self.btn_scan.setEnabled(not run)
        self.btn_del.setEnabled(not run and has)
        self.btn_all.setEnabled(not run and has)
        self.btn_none.setEnabled(not run and has)
        self.chk_dry.setEnabled(not run)
        self.btn_stop.setEnabled(run)
        self.model.editable = not run

    def _set_all(self, on):
        for c in self.model.items:
            c.checked = on
        if self.model.items:
            self.model.dataChanged.emit(self.model.index(0, 0), self.model.index(len(self.model.items) - 1, 0))

    def _open(self, idx):
        c = self.model.items[idx.row()]
        from .gui import open_in_explorer
        open_in_explorer(c.folder if os.path.isdir(c.folder) else os.path.dirname(c.folder))

    def append(self, s):
        self.log.appendPlainText(s)

    # ------------------------------------------------------------------
    def scan(self):
        if not os.path.isdir(self.root):
            QMessageBox.warning(self, "남은 원본 정리", f"작업 폴더에 접근할 수 없습니다:\n{self.root}")
            return
        self.model.set_items([])
        self.pb.setRange(0, 0)
        self.append(f"검색 시작: {self.root}")
        self.scan_th = _ScanThread(self.root)
        self.scan_th.progress.connect(lambda n, p: self.lbl.setText(f"검색 중… 폴더 {n:,}개 · {p}"))
        self.scan_th.done.connect(self._scan_done)
        self.scan_th.start()
        self._update()

    def _scan_done(self, res: LeftoverScan):
        self.pb.setRange(0, max(len(res.candidates), 1))
        self.pb.setValue(0)
        self.model.set_items(res.candidates)
        total = sum(c.total_bytes for c in res.candidates)
        msg = (f"검사 폴더 {res.dirs_scanned:,} · 정리 후보 {len(res.candidates):,}개 ({human_size(total)}) · "
               f"제외 {len(res.skipped):,}" + (" · (중단됨)" if res.cancelled else ""))
        self.lbl.setText(msg)
        self.append("검색 완료: " + msg)
        for p, why in res.skipped[:200]:
            self.append(f"  [제외] {p} — {why}")
        if len(res.skipped) > 200:
            self.append(f"  … 외 {len(res.skipped) - 200:,}건")
        self._update()

    def delete(self):
        if self.busy_check():
            QMessageBox.warning(self, "남은 원본 정리", "압축 작업이 진행 중입니다. 끝난 뒤에 정리하세요.")
            return
        rows = [i for i, c in enumerate(self.model.items) if c.checked]
        if not rows:
            return
        dry = self.chk_dry.isChecked()
        if not dry:
            size = sum(self.model.items[i].total_bytes for i in rows)
            if QMessageBox.warning(
                    self, "원본 삭제 확인",
                    f"{len(rows):,}개 폴더 ({human_size(size)})를 검증한 뒤, 압축파일과 완전히 같은 것만 삭제합니다.\n"
                    "NAS에서는 휴지통으로 가지 않을 수 있습니다. 계속할까요?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
        for i in rows:
            self.model.items[i].status = "대기"
        self.pb.setRange(0, len(rows))
        self.pb.setValue(0)
        self.del_th = _DeleteThread(self.model.items, rows, dry)
        self.del_th.log.connect(self.append)
        self.del_th.started_item.connect(self._started)
        self.del_th.item.connect(self._item)
        self.del_th.done.connect(self._done)
        self.del_th.start()
        self._update()

    def _started(self, r):
        c = self.model.items[r]
        c.status = "진행 중"
        self.model.refresh(r)
        self.lbl.setText(f"검증 중: {c.folder}")
        self.table.scrollTo(self.model.index(r, 0))

    def _item(self, r, ok, msg):
        c = self.model.items[r]
        c.status = ("검증 통과" if self.del_th.dry else "삭제됨") if ok else "보존"
        c.message = msg
        if ok:
            c.checked = False
        self.model.refresh(r)
        self.pb.setValue(self.pb.value() + 1)
        self.append(f"{'✔' if ok else '→'} {c.folder}: {msg}")

    def _done(self, ok_n, keep_n):
        dry = self.del_th.dry
        for c in self.model.items:
            if c.status == "진행 중":
                c.status = "중단"
        self.model.layoutChanged.emit()
        text = (f"{'검증 통과' if dry else '삭제'} {ok_n:,}개 · 보존 {keep_n:,}개")
        self.lbl.setText("완료: " + text)
        self.append("완료: " + text)
        self._update()
        QMessageBox.information(self, "남은 원본 정리", text)

    def stop(self):
        for th in (self.scan_th, self.del_th):
            if th and th.isRunning():
                th.cancel.set()

    def closeEvent(self, ev):
        if self._running():
            self.stop()
            for th in (self.scan_th, self.del_th):
                if th:
                    th.wait(30000)
        ev.accept()
