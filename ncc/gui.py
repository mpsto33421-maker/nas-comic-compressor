"""PySide6 다크 모드 GUI."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

from PySide6.QtCore import (QAbstractTableModel, QRectF, QLockFile, QModelIndex, QSettings,
                            QSortFilterProxyModel, Qt, QThread, QTimer, Signal)
from PySide6.QtGui import (QAction, QBrush, QColor, QFont, QGuiApplication, QIcon, QPainter,
                           QPalette, QPixmap)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox,
                               QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QScrollArea, QSizePolicy,
                               QSystemTrayIcon, QPlainTextEdit,
                               QProgressBar, QPushButton, QSpinBox, QSplitter, QTableView,
                               QVBoxLayout, QWidget)

from . import APP_ID, APP_NAME, __version__
from .bandizip import BandizipRunner, find_bandizip, is_valid_bz
from .compressor import Options, remove_temp_artifact
from .jobs import BatchJob, Callbacks, Summary
from .runlog import RunLog, default_log_dir, human_size
from .scanner import Candidate, ScanResult, Scanner

LEVELS = [("저장 (무압축 · 권장, 이미지는 이미 압축됨)", 0), ("빠른 압축 (-l:1)", 1), ("표준 압축 (-l:5)", 5)]
EXTS = [(".cbz", ".cbz"), (".zip", ".zip")]

STATUS_COLORS = {
    "성공": "#5fd38d", "실패": "#ff6b6b", "건너뜀": "#f0c674", "취소": "#bbbbbb",
    "미실행": "#888888", "진행 중": "#6cb6ff", "대기": "#d0d0d0",
}


def app_base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def open_in_explorer(path: str) -> None:
    try:
        if os.name == "nt":
            os.startfile(path)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError:
        pass


def reveal_in_explorer(path: str) -> None:
    """탐색기에서 파일을 선택한 상태로 연다. 파일이 없으면 상위 폴더를 연다."""
    if os.name == "nt" and os.path.exists(path):
        try:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            return
        except OSError:
            pass
    d = path if os.path.isdir(path) else os.path.dirname(path)
    while d and not os.path.isdir(d):
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    if d:
        open_in_explorer(d)


def work_label(c) -> str:
    """'작품명 › 권' 형태 표시."""
    parent = os.path.basename(c.parent.rstrip("\\/"))
    return f"{parent} › {c.name}" if parent else c.name


# ----------------------------------------------------------------------
class CandidateModel(QAbstractTableModel):
    HEAD = ["", "작품명 / 폴더명", "원본 경로", "이미지 수", "원본 용량", "예상 출력파일", "현재 상태"]

    def __init__(self):
        super().__init__()
        self.items: list[Candidate] = []
        self.editable = True
        self.messages: dict[int, str] = {}

    def set_items(self, items: list[Candidate]):
        self.beginResetModel()
        self.items = items
        self.messages = {}
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEAD)

    def headerData(self, s, o, role=Qt.DisplayRole):
        if o == Qt.Horizontal and role == Qt.DisplayRole:
            return self.HEAD[s]
        return None

    def data(self, idx, role=Qt.DisplayRole):
        if not idx.isValid():
            return None
        c = self.items[idx.row()]
        col = idx.column()
        if role == Qt.CheckStateRole and col == 0:
            return Qt.Checked if c.checked else Qt.Unchecked
        if role == Qt.DisplayRole:
            if col == 1:
                return work_label(c)
            if col == 2:
                return c.path
            if col == 3:
                return c.image_count
            if col == 4:
                return human_size(c.total_bytes)
            if col == 5:
                return os.path.basename(c.output_path)
            if col == 6:
                return c.status + (" ⚠" if c.warnings and c.status == "대기" else "")
        if role == Qt.UserRole:  # 정렬용
            return [c.checked, work_label(c).lower(), c.path.lower(), c.image_count, c.total_bytes,
                    c.output_path.lower(), c.status][col]
        if role == Qt.ToolTipRole:
            tips = list(c.warnings)
            m = self.messages.get(idx.row())
            if m:
                tips.insert(0, m)
            return "\n".join(tips) if tips else c.path
        if role == Qt.ForegroundRole and col == 6:
            return QColor(STATUS_COLORS.get(c.status, "#d0d0d0"))
        if role == Qt.TextAlignmentRole and col in (3, 4):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None

    def flags(self, idx):
        f = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if idx.column() == 0 and self.editable:
            f |= Qt.ItemIsUserCheckable
        return f

    def setData(self, idx, value, role=Qt.EditRole):
        if role == Qt.CheckStateRole and idx.column() == 0 and self.editable:
            self.items[idx.row()].checked = (Qt.CheckState(value) == Qt.Checked)
            self.dataChanged.emit(idx, idx, [Qt.CheckStateRole])
            return True
        return False

    def set_all(self, checked: bool, rows: list[int] | None = None):
        for i in (rows if rows is not None else range(len(self.items))):
            self.items[i].checked = checked
        if self.items:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.items) - 1, 0))

    def refresh_row(self, row: int, message: str | None = None):
        if message is not None:
            self.messages[row] = message
        self.dataChanged.emit(self.index(row, 0), self.index(row, self.columnCount() - 1))


class SortProxy(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.setSortRole(Qt.UserRole)
        self.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.setFilterKeyColumn(2)
        self.status_set: set[str] | None = None

    def set_status_filter(self, statuses):
        self.status_set = set(statuses) if statuses else None
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        if self.status_set is not None:
            items = self.sourceModel().items
            if row < len(items) and items[row].status not in self.status_set:
                return False
        return super().filterAcceptsRow(row, parent)


class StatTile(QFrame):
    def __init__(self, title: str, color: str = "#e3e5ea"):
        super().__init__()
        self.setObjectName("tile")
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 6, 10, 6)
        v.setSpacing(0)
        t = QLabel(title)
        t.setObjectName("tileTitle")
        self.val = QLabel("-")
        self.val.setObjectName("tileValue")
        self.val.setStyleSheet(f"color: {color};")
        v.addWidget(t)
        v.addWidget(self.val)

    def set(self, text: str):
        self.val.setText(text)


def fmt_dur(sec: float) -> str:
    sec = int(max(sec, 0))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ----------------------------------------------------------------------
class ScanThread(QThread):
    progress = Signal(int, str)
    done = Signal(object)

    def __init__(self, root: str, ext: str, min_images: int = 2, skip_cover: bool = True):
        super().__init__()
        self.root, self.ext = root, ext
        self.min_images, self.skip_cover = min_images, skip_cover
        self.cancel_event = threading.Event()
        self._last = 0

    def _prog(self, n, path):
        if n - self._last >= 25 or n == 1:
            self._last = n
            self.progress.emit(n, path)

    def run(self):
        sc = Scanner(self.root, output_ext=self.ext, cancel_event=self.cancel_event, progress=self._prog,
                     min_images=self.min_images, skip_cover_only=self.skip_cover)
        self.done.emit(sc.run())


class CompressThread(QThread):
    log = Signal(str)
    item_started = Signal(int)
    item_finished = Signal(int, str, str)
    overall = Signal(int, int)
    file_progress = Signal(int, int)
    paused = Signal(bool)
    done = Signal(object)

    def __init__(self, root, items, bz, options, scan):
        super().__init__()
        self.runlog = RunLog(kind="run")
        cb = Callbacks(
            log=self.log.emit,
            item_started=lambda i, c: self.item_started.emit(i),
            item_finished=lambda i, r: self.item_finished.emit(i, r.status, r.message + (f" · 원본 {r.original_action}" if r.status == "성공" else "")),
            overall=self.overall.emit,
            file_progress=self.file_progress.emit,
            paused=self.paused.emit,
        )
        self.job = BatchJob(root, items, BandizipRunner(bz), options, self.runlog, cb, scan)

    def run(self):
        try:
            s = self.job.run()
        except Exception as e:  # 안전망
            self.log.emit(f"치명적 오류: {type(e).__name__}: {e}")
            s = self.job.summary
            s.stop_reason = f"치명적 오류: {e}"
        finally:
            self.runlog.close()
        self.done.emit(s)


# ----------------------------------------------------------------------
def make_app_icon(progress: float | None = None) -> QIcon:
    """앱 아이콘을 코드로 그림 (파일 불필요). progress(0~1)가 있으면 하단에 진행 막대 표시."""
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        k = size / 256
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(47, 111, 228))
        p.drawRoundedRect(QRectF(16 * k, 16 * k, 224 * k, 224 * k), 44 * k, 44 * k)
        for y, c in ((70, QColor(255, 255, 255)), (110, QColor(220, 230, 255)), (150, QColor(190, 205, 250))):
            p.setBrush(c)
            p.drawRoundedRect(QRectF(58 * k, y * k, 140 * k, 34 * k), 6 * k, 6 * k)
            p.setBrush(QColor(47, 111, 228))
            p.drawRect(QRectF(66 * k, (y + 6) * k, 12 * k, 22 * k))
        if progress is not None:
            p.setBrush(QColor(20, 22, 28))
            p.drawRect(QRectF(30 * k, 196 * k, 196 * k, 30 * k))
            p.setBrush(QColor(95, 211, 141))
            p.drawRect(QRectF(30 * k, 196 * k, 196 * k * max(0.0, min(progress, 1.0)), 30 * k))
        p.end()
        icon.addPixmap(pm)
    return icon


def instance_server_name() -> str:
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    return f"{APP_ID}-{user}"


# ----------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1320, 900)
        self.settings = QSettings(APP_ID, APP_ID)
        self.scan_result: ScanResult | None = None
        self.scan_thread: ScanThread | None = None
        self.job_thread: CompressThread | None = None
        self.row_of: dict[int, int] = {}  # job index -> model row
        self._really_quit = False
        self._pending_summary: str | None = None
        self.setWindowIcon(make_app_icon())
        self._build()
        self._relax_sizes()
        self._init_paths()
        self._update_buttons()
        self._build_tray()

    # UI ---------------------------------------------------------------
    def _build(self):
        # 창을 아주 작게 줄일 수 있도록 전체 화면을 스크롤 영역에 넣는다.
        # (창이 내용보다 작아지면 스크롤바가 생기고, 크면 내용이 함께 늘어남)
        w = QWidget()
        w.setMinimumSize(0, 0)
        scroll = QScrollArea()
        scroll.setObjectName("mainScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(w)
        self.setCentralWidget(scroll)
        self.setMinimumSize(160, 120)
        root = QVBoxLayout(w)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        title = QLabel(APP_NAME)
        title.setObjectName("title")
        root.addWidget(title)

        top = QFrame()
        top.setObjectName("card")
        g = QGridLayout(top)
        g.addWidget(QLabel("작업 폴더"), 0, 0)
        self.ed_root = QLineEdit()
        self.ed_root.setPlaceholderText(r"예: Y:\만화책  또는  \\NAS\Comics")
        self.ed_root.editingFinished.connect(self._root_edited)
        g.addWidget(self.ed_root, 0, 1)
        b = QPushButton("폴더 선택…")
        b.clicked.connect(self._pick_root)
        g.addWidget(b, 0, 2)
        g.addWidget(QLabel("반디집 bz.exe"), 1, 0)
        self.ed_bz = QLineEdit()
        self.ed_bz.setReadOnly(True)
        g.addWidget(self.ed_bz, 1, 1)
        b = QPushButton("경로 변경…")
        b.clicked.connect(self._pick_bz)
        g.addWidget(b, 1, 2)

        opt = QHBoxLayout()
        opt.addWidget(QLabel("압축 방식"))
        self.cb_level = QComboBox()
        for t, v in LEVELS:
            self.cb_level.addItem(t, v)
        opt.addWidget(self.cb_level)
        opt.addSpacing(12)
        opt.addWidget(QLabel("출력 형식"))
        self.cb_ext = QComboBox()
        for t, v in EXTS:
            self.cb_ext.addItem(t, v)
        self.cb_ext.currentIndexChanged.connect(self._ext_changed)
        opt.addWidget(self.cb_ext)
        opt.addSpacing(12)
        opt.addWidget(QLabel("연속 실패 시 중단"))
        self.sp_fail = QSpinBox()
        self.sp_fail.setRange(0, 1000)
        self.sp_fail.setValue(10)
        self.sp_fail.setSuffix(" 회")
        self.sp_fail.setToolTip("0 = 사용 안 함")
        opt.addWidget(self.sp_fail)
        opt.addSpacing(12)
        self.chk_delete = QCheckBox("압축 성공한 원본 폴더 삭제 (검증 후)")
        self.chk_delete.toggled.connect(self._delete_toggled)
        opt.addWidget(self.chk_delete)
        opt.addStretch(1)
        g.addLayout(opt, 2, 0, 1, 3)
        opt2 = QHBoxLayout()
        opt2.addWidget(QLabel("검색 제외"))
        self.chk_cover = QCheckBox("표지 낱장 제외 (0000/cover 등, 압축파일 옆의 낱장 이미지)")
        self.chk_cover.setChecked(True)
        self.chk_cover.setToolTip("작품 폴더에 1권.zip… 과 함께 있는 0000.jpg/cover.jpg 같은 이미지를\n"
                                  "한 장짜리 압축파일로 만들지 않고 그대로 둡니다.")
        opt2.addWidget(self.chk_cover)
        opt2.addSpacing(12)
        opt2.addWidget(QLabel("최소 이미지 수"))
        self.sp_min = QSpinBox()
        self.sp_min.setRange(1, 1000)
        self.sp_min.setValue(2)
        self.sp_min.setSuffix(" 장")
        self.sp_min.setToolTip("이보다 이미지가 적은 폴더는 압축 대상에서 제외")
        opt2.addWidget(self.sp_min)
        opt2.addStretch(1)
        g.addLayout(opt2, 3, 0, 1, 3)
        g.setColumnStretch(1, 1)
        root.addWidget(top)

        mid = QHBoxLayout()
        self.btn_scan = QPushButton("🔍 압축 대상 검색")
        self.btn_scan.setObjectName("primary")
        self.btn_scan.clicked.connect(self._scan_clicked)
        mid.addWidget(self.btn_scan)
        self.btn_all = QPushButton("전체 선택")
        self.btn_all.clicked.connect(lambda: self._set_all(True))
        self.btn_none = QPushButton("전체 해제")
        self.btn_none.clicked.connect(lambda: self._set_all(False))
        mid.addWidget(self.btn_all)
        mid.addWidget(self.btn_none)
        self.ed_filter = QLineEdit()
        self.ed_filter.setPlaceholderText("경로 필터…")
        self.ed_filter.setMaximumWidth(260)
        mid.addWidget(self.ed_filter)
        self.lbl_scan = QLabel("")
        mid.addWidget(self.lbl_scan, 1)
        self.btn_tmp = QPushButton("임시파일 정리")
        self.btn_tmp.setToolTip("이전 작업이 비정상 종료되어 남은 .ncc-*.tmp 임시파일을 삭제합니다")
        self.btn_tmp.clicked.connect(self._cleanup_temp)
        mid.addWidget(self.btn_tmp)
        b = QPushButton("로그 폴더")
        b.clicked.connect(lambda: open_in_explorer(default_log_dir()))
        mid.addWidget(b)
        root.addLayout(mid)

        # ---- 실시간 작업 현황 ----
        live = QFrame()
        live.setObjectName("live")
        lv = QVBoxLayout(live)
        lv.setContentsMargins(14, 10, 14, 10)
        lv.setSpacing(6)
        head = QHBoxLayout()
        cap = QLabel("실시간 작업 현황")
        cap.setObjectName("liveCap")
        head.addWidget(cap)
        self.lbl_state = QLabel("대기 중")
        self.lbl_state.setObjectName("chip")
        head.addWidget(self.lbl_state)
        head.addStretch(1)
        head.addWidget(QLabel("목록 보기"))
        self.cb_view = QComboBox()
        for t, v in [("전체", None), ("진행 중·대기", ["진행 중", "대기"]),
                     ("실패만", ["실패"]), ("완료(성공)", ["성공"]), ("건너뜀·취소·미실행", ["건너뜀", "취소", "미실행"])]:
            self.cb_view.addItem(t, v)
        self.cb_view.currentIndexChanged.connect(lambda: self.proxy.set_status_filter(self.cb_view.currentData()))
        head.addWidget(self.cb_view)
        self.chk_follow = QCheckBox("진행 중 항목 따라가기")
        self.chk_follow.setChecked(True)
        head.addWidget(self.chk_follow)
        lv.addLayout(head)
        self.lbl_live_name = QLabel("압축 대상을 검색한 뒤 [압축 시작]을 누르세요")
        self.lbl_live_name.setObjectName("liveName")
        self.lbl_live_path = QLabel("")
        self.lbl_live_path.setObjectName("livePath")
        self.lbl_live_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        nrow = QHBoxLayout()
        nrow.addWidget(self.lbl_live_name, 1)
        self.btn_live_src = QPushButton("📂 원본 폴더 보기")
        self.btn_live_src.clicked.connect(lambda: self._live_open("src"))
        self.btn_live_dst = QPushButton("📁 결과 위치 보기")
        self.btn_live_dst.clicked.connect(lambda: self._live_open("dst"))
        for b in (self.btn_live_src, self.btn_live_dst):
            b.setEnabled(False)
            nrow.addWidget(b)
        lv.addLayout(nrow)
        lv.addWidget(self.lbl_live_path)
        tiles = QHBoxLayout()
        tiles.setSpacing(8)
        self.t_prog = StatTile("진행")
        self.t_ok = StatTile("성공", STATUS_COLORS["성공"])
        self.t_fail = StatTile("실패", STATUS_COLORS["실패"])
        self.t_skip = StatTile("건너뜀", STATUS_COLORS["건너뜀"])
        self.t_bytes = StatTile("처리 용량")
        self.t_speed = StatTile("속도")
        self.t_time = StatTile("경과 / 남은 예상")
        for t in (self.t_prog, self.t_ok, self.t_fail, self.t_skip, self.t_bytes, self.t_speed, self.t_time):
            tiles.addWidget(t, 1)
        lv.addLayout(tiles)
        self.lbl_last = QLabel("")
        self.lbl_last.setObjectName("livePath")
        lv.addWidget(self.lbl_last)
        root.addWidget(live)
        self.live_timer = QTimer(self)
        self.live_timer.setInterval(1000)
        self.live_timer.timeout.connect(self._refresh_live)
        self._live_reset(0, 0)
        self._set_state("대기 중", "#8b93a3")

        self.model = CandidateModel()
        self.proxy = SortProxy()
        self.proxy.setSourceModel(self.model)
        self.ed_filter.textChanged.connect(self.proxy.setFilterFixedString)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(24)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.Interactive)
        h.setStretchLastSection(False)
        for i, wdt in enumerate([30, 240, 460, 80, 95, 220, 130]):
            self.table.setColumnWidth(i, wdt)
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.doubleClicked.connect(self._open_row)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(20000)
        self.log.setFont(QFont("Consolas", 9))

        split = QSplitter(Qt.Vertical)
        split.addWidget(self.table)
        split.addWidget(self.log)
        split.setSizes([520, 220])
        root.addWidget(split, 1)

        bottom = QFrame()
        bottom.setObjectName("card")
        bl = QGridLayout(bottom)
        self.btn_start = QPushButton("▶ 압축 시작")
        self.btn_start.setObjectName("primary")
        self.btn_start.clicked.connect(self._start)
        self.btn_pause = QPushButton("⏸ 일시정지")
        self.btn_pause.clicked.connect(self._pause)
        self.btn_stop = QPushButton("⏹ 중단")
        self.btn_stop.setObjectName("danger")
        self.btn_stop.clicked.connect(self._stop)
        bl.addWidget(self.btn_start, 0, 0, 2, 1)
        bl.addWidget(self.btn_pause, 0, 1, 2, 1)
        bl.addWidget(self.btn_stop, 0, 2, 2, 1)
        bl.addWidget(QLabel("전체 진행률"), 0, 3)
        self.pb_all = QProgressBar()
        self.pb_all.setFormat("%v / %m  (%p%)")
        bl.addWidget(self.pb_all, 0, 4)
        self.lbl_cur = QLabel("현재 파일")
        bl.addWidget(self.lbl_cur, 1, 3)
        self.pb_cur = QProgressBar()
        self.pb_cur.setFormat("%v / %m")
        bl.addWidget(self.pb_cur, 1, 4)
        bl.setColumnStretch(4, 1)
        self.lbl_status = QLabel("준비")
        self.lbl_status.setObjectName("status")
        bl.addWidget(self.lbl_status, 2, 0, 1, 5)
        root.addWidget(bottom)

    def _relax_sizes(self):
        """긴 텍스트 라벨/입력칸이 창의 최소 폭을 키우지 않도록 함."""
        loose = QSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        for lbl in (self.lbl_scan, self.lbl_status, self.lbl_live_name, self.lbl_live_path,
                    self.lbl_last, self.lbl_cur):
            lbl.setSizePolicy(loose)
            lbl.setMinimumWidth(0)
        for wdg in (self.ed_root, self.ed_bz, self.ed_filter, self.cb_level, self.cb_view):
            wdg.setMinimumWidth(40)
            sp = wdg.sizePolicy()
            sp.setHorizontalPolicy(QSizePolicy.Ignored if wdg in (self.ed_root, self.ed_bz) else QSizePolicy.Preferred)
            wdg.setSizePolicy(sp)
        self.cb_level.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cb_level.setMinimumContentsLength(14)
        self.table.setMinimumSize(80, 60)
        self.log.setMinimumSize(80, 30)
        for t in (self.t_prog, self.t_ok, self.t_fail, self.t_skip, self.t_bytes, self.t_speed, self.t_time):
            t.setMinimumWidth(0)
            t.setSizePolicy(QSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred))
        self.pb_all.setMinimumWidth(30)
        self.pb_cur.setMinimumWidth(30)

    # 초기화 -------------------------------------------------------------
    def _init_paths(self):
        self.ed_root.setText(app_base_dir())
        bz = self.settings.value("bz_path", "", str)
        if not is_valid_bz(bz):
            bz = find_bandizip() or ""
        self._set_bz(bz)
        lv = self.settings.value("level", 0, int)
        i = self.cb_level.findData(lv)
        self.cb_level.setCurrentIndex(max(i, 0))
        ext = self.settings.value("ext", ".cbz", str)
        self.cb_ext.blockSignals(True)
        self.cb_ext.setCurrentIndex(max(self.cb_ext.findData(ext), 0))
        self.cb_ext.blockSignals(False)
        self.sp_fail.setValue(self.settings.value("max_fail", 10, int))
        self.sp_min.setValue(self.settings.value("min_images", 2, int))
        self.chk_cover.setChecked(self.settings.value("skip_cover", True, bool))
        self.append_log(f"{APP_NAME} {__version__} 시작 · 작업 폴더: {self.ed_root.text()}")
        self.append_log(f"로그 저장 위치: {default_log_dir()}")
        if not bz:
            self.append_log("⚠ 반디집(bz.exe)을 찾지 못했습니다. [경로 변경…]으로 직접 선택하세요.")

    def _set_bz(self, path: str):
        self.bz_path = path
        self.ed_bz.setText(path or "(찾을 수 없음 - 반디집 설치 또는 경로 선택 필요)")
        if path:
            self.settings.setValue("bz_path", path)
        self._update_buttons()

    # 이벤트 -------------------------------------------------------------
    def append_log(self, s: str):
        self.log.appendPlainText(s)

    def _busy(self) -> bool:
        return bool((self.scan_thread and self.scan_thread.isRunning()) or
                    (self.job_thread and self.job_thread.isRunning()))

    def _update_buttons(self):
        scanning = bool(self.scan_thread and self.scan_thread.isRunning())
        running = bool(self.job_thread and self.job_thread.isRunning())
        has = bool(self.model.items)
        self.btn_scan.setText("⏹ 검색 중단" if scanning else "🔍 압축 대상 검색")
        self.btn_scan.setEnabled(not running)
        self.btn_start.setEnabled(not scanning and not running and has and bool(self.bz_path))
        self.btn_pause.setEnabled(running)
        self.btn_stop.setEnabled(running)
        for b in (self.btn_all, self.btn_none):
            b.setEnabled(has and not running)
        self.btn_tmp.setEnabled(not scanning and not running and bool(
            self.scan_result and self.scan_result.temp_artifacts))
        for wdg in (self.ed_root, self.cb_ext, self.cb_level, self.chk_delete, self.sp_fail,
                    self.sp_min, self.chk_cover):
            wdg.setEnabled(not running and not scanning)
        self.model.editable = not running
        if getattr(self, "tray", None) is not None:
            self._update_tray()

    def _pick_root(self):
        d = QFileDialog.getExistingDirectory(self, "작업 폴더 선택", self.ed_root.text())
        if d:
            self.ed_root.setText(os.path.normpath(d))
            self._root_edited(force=True)

    def _root_edited(self, force: bool = False):
        # 작업 시작 시 입력칸이 비활성화되며 editingFinished 가 발생해도 목록을 지우지 않음
        if self._busy():
            return
        cur = os.path.normcase(os.path.normpath(self.ed_root.text().strip().strip('"') or "."))
        if not force and self.scan_result is not None and \
                cur == os.path.normcase(os.path.normpath(self.scan_result.root)):
            return
        self.model.set_items([])
        self.scan_result = None
        self.lbl_scan.setText("")
        self._update_buttons()

    def _pick_bz(self):
        start = os.path.dirname(self.bz_path) if self.bz_path else os.environ.get("ProgramFiles", "C:\\")
        f, _ = QFileDialog.getOpenFileName(self, "반디집 bz.exe 선택", start, "bz.exe (bz.exe);;실행파일 (*.exe)")
        if f:
            if not is_valid_bz(f):
                QMessageBox.warning(self, APP_NAME, "반디집 콘솔 도구 bz.exe 를 선택하세요.\n(보통 C:\\Program Files\\Bandizip\\bz.exe)")
                return
            self._set_bz(os.path.normpath(f))

    def _ext_changed(self):
        self.settings.setValue("ext", self.cb_ext.currentData())
        if self.model.items:
            self.append_log("출력 형식이 바뀌어 목록을 비웠습니다. 다시 검색하세요.")
            self._root_edited(force=True)

    def _delete_toggled(self, on: bool):
        if on:
            r = QMessageBox.warning(
                self, "원본 삭제 경고",
                "압축 성공 후 원본 이미지 폴더를 삭제합니다.\n\n"
                "삭제 조건 (하나라도 만족하지 않으면 보존):\n"
                " • 반디집 무결성 검사 통과\n"
                " • 압축파일 내부 목록·크기가 원본과 일치\n"
                " • 원본 파일과 압축파일 내부 파일의 SHA-256 해시 일치\n"
                " • 폴더 안에 이미지 외 파일/하위 폴더가 없음\n"
                " • 검색 이후 원본이 바뀌지 않음\n\n"
                "NAS에서 삭제한 파일은 휴지통으로 가지 않을 수 있습니다.\n계속할까요?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                self.chk_delete.blockSignals(True)
                self.chk_delete.setChecked(False)
                self.chk_delete.blockSignals(False)

    def _set_all(self, on: bool):
        if self.ed_filter.text():
            rows = [self.proxy.mapToSource(self.proxy.index(r, 0)).row() for r in range(self.proxy.rowCount())]
            self.model.set_all(on, rows)
        else:
            self.model.set_all(on)
        self._update_sel_label()

    def _update_sel_label(self):
        if not self.model.items:
            return
        sel = [c for c in self.model.items if c.checked]
        self.lbl_status.setText(f"선택 {len(sel):,} / {len(self.model.items):,}개 · {human_size(sum(c.total_bytes for c in sel))}")

    def _open_row(self, pidx):
        c = self.model.items[self.proxy.mapToSource(pidx).row()]
        open_in_explorer(c.path if os.path.isdir(c.path) else c.parent)

    def _live_open(self, which: str):
        r = self.live.get("cur")
        if r is None or r >= len(self.model.items):
            return
        c = self.model.items[r]
        if which == "src":
            open_in_explorer(c.path if os.path.isdir(c.path) else c.parent)
        else:
            reveal_in_explorer(c.output_path)

    def _selected_rows(self) -> list[int]:
        rows = {self.proxy.mapToSource(i).row() for i in self.table.selectionModel().selectedRows()}
        return sorted(rows)

    def _table_menu(self, pos):
        pidx = self.table.indexAt(pos)
        if not pidx.isValid():
            return
        r = self.proxy.mapToSource(pidx).row()
        if r not in self._selected_rows():
            self.table.selectRow(pidx.row())
        rows = self._selected_rows() or [r]
        c = self.model.items[r]
        m = QMenu(self)
        a = m.addAction("📂 원본 폴더 열기")
        a.setEnabled(os.path.isdir(c.path))
        a.triggered.connect(lambda: open_in_explorer(c.path))
        a = m.addAction("📁 작품(상위) 폴더 열기")
        a.triggered.connect(lambda: open_in_explorer(c.parent))
        out_exists = os.path.exists(c.output_path)
        a = m.addAction("🗂 결과 파일 위치 열기" if out_exists else "🗂 결과 파일 위치 열기 (아직 없음)")
        a.triggered.connect(lambda: reveal_in_explorer(c.output_path))
        a = m.addAction("📖 결과 파일 열기")
        a.setEnabled(out_exists)
        a.triggered.connect(lambda: open_in_explorer(c.output_path))
        m.addSeparator()
        cb = QGuiApplication.clipboard()
        m.addAction("원본 경로 복사").triggered.connect(
            lambda: cb.setText("\n".join(self.model.items[i].path for i in rows)))
        m.addAction("결과 파일 경로 복사").triggered.connect(
            lambda: cb.setText("\n".join(self.model.items[i].output_path for i in rows)))
        msg = self.model.messages.get(r) or "\n".join(c.warnings)
        a = m.addAction("상태 메시지 복사")
        a.setEnabled(bool(msg))
        a.triggered.connect(lambda: cb.setText(msg))
        if self.model.editable:
            m.addSeparator()
            m.addAction(f"선택 {len(rows)}개 체크").triggered.connect(lambda: self._check_rows(rows, True))
            m.addAction(f"선택 {len(rows)}개 체크 해제").triggered.connect(lambda: self._check_rows(rows, False))
        m.exec(self.table.viewport().mapToGlobal(pos))

    def _check_rows(self, rows, on):
        self.model.set_all(on, rows)
        self._update_sel_label()

    # 검색 ---------------------------------------------------------------
    def _scan_clicked(self):
        if self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.cancel_event.set()
            return
        root = self.ed_root.text().strip().strip('"')
        if not root or not os.path.isdir(root):
            QMessageBox.warning(self, APP_NAME, f"작업 폴더에 접근할 수 없습니다:\n{root}")
            return
        self.model.set_items([])
        self.append_log(f"검색 시작: {root}")
        self.lbl_status.setText("검색 중…")
        self.pb_all.setRange(0, 0)
        self.settings.setValue("min_images", self.sp_min.value())
        self.settings.setValue("skip_cover", self.chk_cover.isChecked())
        self.scan_thread = ScanThread(root, self.cb_ext.currentData(), self.sp_min.value(), self.chk_cover.isChecked())
        self.scan_thread.progress.connect(lambda n, p: self.lbl_status.setText(f"검색 중… 폴더 {n:,}개 · {p}"))
        self.scan_thread.done.connect(self._scan_done)
        self.scan_thread.start()
        self._update_buttons()

    def _scan_done(self, res: ScanResult):
        self.pb_all.setRange(0, 1)
        self.pb_all.setValue(0)
        self.scan_result = res
        for c in res.candidates:
            c.checked = True
        self.model.set_items(res.candidates)
        self.table.sortByColumn(2, Qt.AscendingOrder)
        sk, er = len(res.skipped), len(res.errors)
        msg = (f"검사 폴더 {res.dirs_scanned:,} · 압축 대상 {len(res.candidates):,} · 건너뜀 {sk:,} · 오류 {er:,}"
               + (" · (중단됨)" if res.cancelled else ""))
        self.lbl_scan.setText(msg)
        self.append_log("검색 완료: " + msg)
        # 검색 기록 파일
        try:
            sl = RunLog(kind="scan")
            for r in res.records:
                sl.row("검색", r.path, r.status, r.reason)
            sl.close()
            self.append_log(f"검색 기록: {sl.csv_path}")
        except OSError as e:
            self.append_log(f"검색 기록 저장 실패: {e}")
        shown = 0
        for r in res.records:
            if r.status in ("건너뜀", "오류"):
                if shown < 300:
                    self.append_log(f"  [{r.status}] {r.path} — {r.reason}")
                shown += 1
        if shown > 300:
            self.append_log(f"  … 외 {shown - 300:,}건 (전체는 검색 기록 CSV 참고)")
        if res.temp_artifacts:
            self.append_log(f"⚠ 이전 작업의 임시파일 {len(res.temp_artifacts)}개 발견 → [임시파일 정리]로 삭제할 수 있습니다")
        warn = sum(1 for c in res.candidates if c.warnings)
        if warn:
            self.append_log(f"참고: 경고가 있는 대상 {warn}개 (상태 열 ⚠, 마우스를 올리면 내용 표시)")
        self._update_buttons()
        self._update_sel_label()

    def _cleanup_temp(self):
        if not self.scan_result or not self.scan_result.temp_artifacts:
            return
        lst = self.scan_result.temp_artifacts
        preview = "\n".join(lst[:15]) + (f"\n… 외 {len(lst) - 15}개" if len(lst) > 15 else "")
        if QMessageBox.question(self, "임시파일 정리",
                                f"다음 임시파일/폴더 {len(lst)}개를 삭제합니다. (원본 이미지는 건드리지 않음)\n\n{preview}") != QMessageBox.Yes:
            return
        ok = fail = 0
        for p in list(lst):
            try:
                remove_temp_artifact(p)
                lst.remove(p)
                ok += 1
            except OSError as e:
                fail += 1
                self.append_log(f"  임시파일 삭제 실패: {p} ({e})")
        self.append_log(f"임시파일 정리: 삭제 {ok}개, 실패 {fail}개")
        self._update_buttons()

    # 압축 ---------------------------------------------------------------
    def _start(self):
        if not is_valid_bz(self.bz_path):
            QMessageBox.warning(self, APP_NAME, "반디집 bz.exe 경로를 먼저 지정하세요.")
            return
        rows = [i for i, c in enumerate(self.model.items) if c.checked]
        if not rows:
            QMessageBox.information(self, APP_NAME, "선택된 압축 대상이 없습니다.")
            return
        delete = self.chk_delete.isChecked()
        total_bytes = sum(self.model.items[i].total_bytes for i in rows)
        txt = (f"{len(rows):,}개 폴더 ({human_size(total_bytes)})를 압축합니다.\n"
               f"원본 처리: {'검증 후 삭제' if delete else '보존'}\n\n시작할까요?")
        if QMessageBox.question(self, "압축 시작", txt) != QMessageBox.Yes:
            return
        for c in self.model.items:
            if c.status not in ("성공",):
                c.status = "대기"
        self.row_of = {j: r for j, r in enumerate(rows)}
        items = [self.model.items[i] for i in rows]
        opt = Options(level=self.cb_level.currentData(), delete_originals=delete,
                      max_consecutive_failures=self.sp_fail.value())
        self.settings.setValue("level", opt.level)
        self.settings.setValue("max_fail", opt.max_consecutive_failures)
        th = CompressThread(self.ed_root.text().strip(), items, self.bz_path, opt, self.scan_result)
        th.log.connect(self.append_log)
        th.item_started.connect(self._item_started)
        th.item_finished.connect(self._item_finished)
        th.overall.connect(self._overall)
        th.file_progress.connect(self._file_progress)
        th.paused.connect(self._paused)
        th.done.connect(self._job_done)
        self.job_thread = th
        self.pb_all.setRange(0, len(items))
        self.pb_all.setValue(0)
        self.append_log(f"작업 로그: {th.runlog.log_path}")
        self._live_reset(len(items), sum(c.total_bytes for c in items))
        self.live_timer.start()
        th.start()
        self._update_buttons()
        self._refresh_all_rows()

    def _refresh_all_rows(self):
        if self.model.items:
            self.model.dataChanged.emit(self.model.index(0, 0),
                                        self.model.index(len(self.model.items) - 1, self.model.columnCount() - 1))
        self.proxy.invalidateFilter()

    def _live_reset(self, total: int, total_bytes: int):
        self.live = {"total": total, "total_bytes": total_bytes, "done": 0, "ok": 0, "fail": 0,
                     "skip": 0, "bytes": 0, "ok_bytes": 0, "start": time.monotonic(), "cur": None}
        self._refresh_live()

    def _refresh_live(self):
        if getattr(self, "tray", None) is not None:
            self._update_tray()
        L = self.live
        el = time.monotonic() - L["start"] if L["total"] else 0
        self.t_prog.set(f"{L['done']:,} / {L['total']:,}")
        self.t_ok.set(f"{L['ok']:,}")
        self.t_fail.set(f"{L['fail']:,}")
        self.t_skip.set(f"{L['skip']:,}")
        self.t_bytes.set(f"{human_size(L['bytes'])} / {human_size(L['total_bytes'])}" if L["total"] else "-")
        speed = L["bytes"] / el if el > 1 else 0
        self.t_speed.set(f"{human_size(speed)}/s" if speed else "-")
        if L["bytes"] and el > 1 and L["done"] < L["total"]:
            remain = (L["total_bytes"] - L["bytes"]) / max(speed, 1)
            self.t_time.set(f"{fmt_dur(el)} / {fmt_dur(remain)}")
        else:
            self.t_time.set(fmt_dur(el) if L["total"] else "-")

    def _set_state(self, text: str, color: str):
        self.lbl_state.setText(text)
        self.lbl_state.setStyleSheet(f"background: {color}; color: #10131a;")

    def _item_started(self, j):
        r = self.row_of.get(j)
        if r is None:
            return
        c = self.model.items[r]
        self.live["cur"] = r
        self.model.refresh_row(r)
        self.lbl_cur.setText(f"현재: {c.name}"[:60])
        self.lbl_status.setText(f"압축 중: {c.path}")
        self.lbl_live_name.setText(f"📦 {work_label(c)}  →  {os.path.basename(c.output_path)}")
        self.btn_live_src.setEnabled(True)
        self.btn_live_dst.setEnabled(True)
        self.lbl_live_path.setText(f"{c.path}   ·   이미지 {c.image_count:,}개 · {human_size(c.total_bytes)}")
        if not (self.job_thread and self.job_thread.job.is_paused):
            self._set_state("진행 중", STATUS_COLORS["진행 중"])
        if self.chk_follow.isChecked():
            src = self.proxy.mapFromSource(self.model.index(r, 0))
            if src.isValid():
                self.table.scrollTo(src, QAbstractItemView.PositionAtCenter)
                self.table.selectRow(src.row())

    def _item_finished(self, j, status, msg):
        r = self.row_of.get(j)
        L = self.live
        if status in ("성공", "실패", "건너뜀"):
            L["done"] += 1
            if r is not None:
                L["bytes"] += self.model.items[r].total_bytes
        if status == "성공":
            L["ok"] += 1
        elif status == "실패":
            L["fail"] += 1
        elif status == "건너뜀":
            L["skip"] += 1
        if r is not None:
            self.model.refresh_row(r, msg)
            if status in ("성공", "실패", "건너뜀", "취소"):
                mark = {"성공": "✔", "실패": "✖", "건너뜀": "→", "취소": "■"}[status]
                self.lbl_last.setText(f"직전: {mark} {self.model.items[r].name} — {msg}"[:220])
        if self.proxy.status_set is not None:
            self.proxy.invalidateFilter()
        self._refresh_live()

    def _overall(self, d, t):
        self.pb_all.setMaximum(max(t, 1))
        self.pb_all.setValue(d)

    def _file_progress(self, d, t):
        self.pb_cur.setMaximum(max(t, 1))
        self.pb_cur.setValue(d)

    def _paused(self, p):
        self._update_tray()
        self.btn_pause.setText("▶ 계속" if p else "⏸ 일시정지")
        if p:
            self.lbl_status.setText("일시정지됨")
            self._set_state("일시정지", "#f0c674")
        else:
            self._set_state("진행 중", STATUS_COLORS["진행 중"])

    def _pause(self):
        if not self.job_thread:
            return
        job = self.job_thread.job
        if job.is_paused:
            job.resume()
            self.btn_pause.setText("⏸ 일시정지")
        else:
            job.pause()
            self.btn_pause.setText("▶ 계속")
            self.lbl_status.setText("일시정지 요청됨 – 현재 항목을 마친 뒤 멈춥니다")

    def _stop(self):
        if not self.job_thread:
            return
        if QMessageBox.question(self, "중단", "작업을 중단할까요?\n진행 중인 항목은 취소되고 임시파일은 삭제되며 원본은 보존됩니다.") == QMessageBox.Yes:
            self.job_thread.job.cancel()
            self.lbl_status.setText("중단 중…")
            self._set_state("중단 중", "#ff6b6b")

    def _job_done(self, s: Summary):
        self.btn_pause.setText("⏸ 일시정지")
        for c in self.model.items:
            if c.status in ("성공", "건너뜀"):
                c.checked = False  # 다시 시작해도 완료된 항목은 제외
        self._refresh_all_rows()
        self.live_timer.stop()
        self._refresh_live()
        self._set_state("완료" if not s.stop_reason else "종료", STATUS_COLORS["성공"] if not s.stop_reason else "#f0c674")
        self.lbl_live_name.setText("작업 완료" if not s.stop_reason else f"작업 종료 — {s.stop_reason}")
        self.lbl_live_path.setText(f"성공 {s.success:,} · 실패 {s.failed:,} · 건너뜀 {s.skipped:,} · "
                                   f"미실행 {s.not_run + s.cancelled:,} · 결과 {human_size(s.out_bytes)}")
        th = self.job_thread
        self.job_thread = None
        self._update_buttons()
        self.lbl_status.setText("완료" if not s.stop_reason else f"종료: {s.stop_reason}")
        text = "\n".join(s.lines())
        if th:
            text += f"\n\n결과 CSV:\n{th.runlog.csv_path}"
        self._update_tray()
        if self.tray is not None and not self.isVisible():
            self._pending_summary = text
            self.tray.showMessage(
                "압축 완료" if not s.stop_reason else "작업 종료",
                f"성공 {s.success:,} · 실패 {s.failed:,} · 건너뜀 {s.skipped:,}"
                + (f"\n{s.stop_reason}" if s.stop_reason else "") + "\n클릭하면 결과를 봅니다.",
                QSystemTrayIcon.Information if not s.stop_reason else QSystemTrayIcon.Warning, 10000)
        else:
            QMessageBox.information(self, "작업 결과", text)

    # 시스템 트레이 ------------------------------------------------------
    def _build_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(make_app_icon(), self)
        self.tray.setToolTip(APP_NAME)
        m = QMenu()
        self.act_show = m.addAction("열기")
        self.act_show.triggered.connect(self.show_from_tray)
        m.addSeparator()
        self.act_status = m.addAction("대기 중")
        self.act_status.setEnabled(False)
        self.act_pause = m.addAction("일시정지")
        self.act_pause.triggered.connect(self._pause)
        self.act_stop = m.addAction("중단")
        self.act_stop.triggered.connect(self._stop_from_tray)
        m.addSeparator()
        self.act_close_to_tray = m.addAction("X 버튼으로 닫을 때 트레이로 보내기")
        self.act_close_to_tray.setCheckable(True)
        self.act_close_to_tray.setChecked(self.settings.value("close_to_tray", True, bool))
        self.act_close_to_tray.toggled.connect(lambda v: self.settings.setValue("close_to_tray", v))
        m.addSeparator()
        m.addAction("종료").triggered.connect(self.quit_app)
        self.tray_menu = m
        self.tray.setContextMenu(m)
        self.tray.activated.connect(self._tray_activated)
        self.tray.messageClicked.connect(self.show_from_tray)
        self.tray.show()
        self._tray_icon_step = -1
        self._update_tray()

    def _tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            if self.isVisible() and not self.isMinimized() and reason == QSystemTrayIcon.Trigger:
                self.activateWindow()
                self.raise_()
            else:
                self.show_from_tray()

    def show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()
        if self._pending_summary:
            text, self._pending_summary = self._pending_summary, None
            QTimer.singleShot(200, lambda: QMessageBox.information(self, "작업 결과", text))

    def _stop_from_tray(self):
        self._stop()

    def _update_tray(self):
        if not self.tray:
            return
        running = bool(self.job_thread and self.job_thread.isRunning())
        scanning = bool(self.scan_thread and self.scan_thread.isRunning())
        L = getattr(self, "live", None)
        if running and L and L["total"]:
            pct = L["done"] / L["total"]
            paused = self.job_thread.job.is_paused
            head = "일시정지" if paused else "압축 중"
            line = (f"{head} {L['done']:,}/{L['total']:,} ({pct:.0%}) · 성공 {L['ok']:,} · "
                    f"실패 {L['fail']:,} · 건너뜀 {L['skip']:,}")
            cur = L.get("cur")
            name = work_label(self.model.items[cur]) if cur is not None and cur < len(self.model.items) else ""
            tip = f"{APP_NAME}\n{line}" + (f"\n{name}" if name else "")
            step = int(pct * 20)
            if step != self._tray_icon_step:
                self._tray_icon_step = step
                self.tray.setIcon(make_app_icon(pct))
            self.act_status.setText(line)
            self.act_pause.setText("계속" if paused else "일시정지")
        else:
            line = "검색 중…" if scanning else "대기 중"
            tip = f"{APP_NAME}\n{line}"
            if self._tray_icon_step != -1:
                self._tray_icon_step = -1
                self.tray.setIcon(make_app_icon())
            self.act_status.setText(line)
            self.act_pause.setText("일시정지")
        self.act_pause.setEnabled(running)
        self.act_stop.setEnabled(running)
        self.tray.setToolTip(tip[:127])

    def quit_app(self):
        """트레이 메뉴의 '종료' — 실제로 프로그램을 끝냄."""
        self._really_quit = True
        if not self.isVisible() and self.job_thread and self.job_thread.isRunning():
            self.show_from_tray()
        if self.close():
            QApplication.quit()
        else:
            self._really_quit = False

    def handle_instance_message(self, msg: str):
        if msg == "show":
            self.show_from_tray()

    # 종료 ---------------------------------------------------------------
    def closeEvent(self, ev):
        if (not self._really_quit and self.tray is not None
                and self.act_close_to_tray.isChecked()):
            ev.ignore()
            self.hide()
            if not self.settings.value("tray_hint_shown", False, bool):
                self.settings.setValue("tray_hint_shown", True)
                self.tray.showMessage(APP_NAME, "창을 닫아도 작업 표시줄 오른쪽 트레이에서 계속 실행됩니다.\n"
                                      "트레이 아이콘을 클릭하면 다시 열리고, 완전히 끄려면 우클릭 → 종료를 누르세요.",
                                      QSystemTrayIcon.Information, 6000)
            return
        if self.job_thread and self.job_thread.isRunning():
            if QMessageBox.question(self, APP_NAME,
                                    "압축 작업이 진행 중입니다.\n중단하고 종료할까요? (진행 중 항목의 임시파일은 삭제, 원본은 보존)") != QMessageBox.Yes:
                ev.ignore()
                return
            self.job_thread.job.cancel()
            self.lbl_status.setText("작업을 정리하는 중…")
            QApplication.processEvents()
            if not self.job_thread.wait(30000):
                QMessageBox.warning(self, APP_NAME, "작업 정리가 지연되고 있습니다. 잠시 후 다시 종료하세요.")
                ev.ignore()
                return
        if self.scan_thread and self.scan_thread.isRunning():
            self.scan_thread.cancel_event.set()
            self.scan_thread.wait(10000)
        if self.tray is not None:
            self.tray.hide()
        ev.accept()


# ----------------------------------------------------------------------
DARK_QSS = """
* { font-family: 'Malgun Gothic', 'Segoe UI', sans-serif; font-size: 10pt; }
QMainWindow, QWidget { background: #1e2026; color: #e3e5ea; }
QLabel#title { font-size: 17pt; font-weight: 700; color: #ffffff; padding: 2px 2px 4px 2px; }
QLabel#status { color: #9aa3b2; }
QFrame#card { background: #262a32; border: 1px solid #323743; border-radius: 8px; }
QFrame#card QLabel { background: transparent; }
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit {
  background: #15171c; border: 1px solid #3a404d; border-radius: 5px; padding: 4px 6px; color: #e3e5ea;
  selection-background-color: #3d6fd8; }
QLineEdit:read-only { color: #aab2c0; }
QPushButton { background: #323844; border: 1px solid #434b5a; border-radius: 6px; padding: 6px 14px; color: #e8eaee; }
QPushButton:hover { background: #3c4352; }
QPushButton:disabled { color: #6b7280; background: #2a2e36; border-color: #333844; }
QPushButton#primary { background: #2f6fe4; border-color: #3b7cf0; font-weight: 600; }
QPushButton#primary:hover { background: #3b7cf0; }
QPushButton#primary:disabled { background: #2a3550; color: #7d8aa8; }
QPushButton#danger { background: #8a2e35; border-color: #a33a43; }
QPushButton#danger:disabled { background: #3a2a2d; color: #7b6a6d; }
QTableView { background: #17191e; alternate-background-color: #1c1f25; gridline-color: #2b2f38;
  border: 1px solid #323743; border-radius: 6px; selection-background-color: #2c4a7e; }
QHeaderView::section { background: #262a32; color: #c3c8d2; border: none; border-right: 1px solid #323743;
  border-bottom: 1px solid #323743; padding: 5px; }
QProgressBar { background: #15171c; border: 1px solid #3a404d; border-radius: 5px; text-align: center; height: 18px; }
QProgressBar::chunk { background: #2f6fe4; border-radius: 4px; }
QCheckBox { spacing: 6px; }
QToolTip { background: #2b2f38; color: #e3e5ea; border: 1px solid #454c5a; }
QFrame#live { background: #20242c; border: 1px solid #2f6fe4; border-radius: 8px; }
QFrame#live QLabel { background: transparent; }
QLabel#liveCap { font-weight: 700; color: #9cc0ff; }
QLabel#chip { background: #3a404d; color: #10131a; border-radius: 9px; padding: 1px 10px; font-weight: 700; }
QLabel#liveName { font-size: 13pt; font-weight: 700; color: #ffffff; }
QLabel#livePath { color: #9aa3b2; }
QFrame#tile { background: #15171c; border: 1px solid #2f3540; border-radius: 6px; }
QLabel#tileTitle { color: #8b93a3; font-size: 8.5pt; }
QLabel#tileValue { font-size: 12pt; font-weight: 700; }
QSplitter::handle { background: #1e2026; height: 6px; }
QScrollArea#mainScroll { background: #1e2026; border: none; }
QScrollBar:vertical, QScrollBar:horizontal { background: #1e2026; }
"""


def apply_dark(app: QApplication):
    app.setStyle("Fusion")
    p = QPalette()
    for role, col in [
        (QPalette.Window, "#1e2026"), (QPalette.WindowText, "#e3e5ea"), (QPalette.Base, "#17191e"),
        (QPalette.AlternateBase, "#1c1f25"), (QPalette.Text, "#e3e5ea"), (QPalette.Button, "#323844"),
        (QPalette.ButtonText, "#e8eaee"), (QPalette.Highlight, "#2f6fe4"), (QPalette.HighlightedText, "#ffffff"),
        (QPalette.ToolTipBase, "#2b2f38"), (QPalette.ToolTipText, "#e3e5ea"),
    ]:
        p.setColor(role, QColor(col))
    app.setPalette(p)
    app.setStyleSheet(DARK_QSS)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    apply_dark(app)
    app.setWindowIcon(make_app_icon())
    lock = QLockFile(os.path.join(os.path.dirname(default_log_dir()), "instance.lock"))
    if not lock.tryLock(100):
        # 이미 실행 중(트레이에 있을 수 있음) → 기존 창을 띄우고 종료
        sock = QLocalSocket()
        sock.connectToServer(instance_server_name())
        if sock.waitForConnected(1000):
            sock.write(b"show")
            sock.flush()
            sock.waitForBytesWritten(1000)
            sock.disconnectFromServer()
            return 0
        QMessageBox.warning(None, APP_NAME, "프로그램이 이미 실행 중입니다. (작업 표시줄 오른쪽 트레이를 확인하세요)")
        return 1
    app.setQuitOnLastWindowClosed(False)
    w = MainWindow()
    server = QLocalServer()
    QLocalServer.removeServer(instance_server_name())
    if server.listen(instance_server_name()):
        def on_conn():
            c = server.nextPendingConnection()
            if c is None:
                return
            def read():
                w.handle_instance_message(bytes(c.readAll()).decode(errors="ignore").strip() or "show")
            c.readyRead.connect(read)
            c.disconnected.connect(c.deleteLater)
        server.newConnection.connect(on_conn)
    if w.tray is None:
        app.setQuitOnLastWindowClosed(True)
    w.show()
    QTimer.singleShot(0, w._update_sel_label)
    rc = app.exec()
    server.close()
    lock.unlock()
    return rc
