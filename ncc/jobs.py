"""여러 항목을 순차 처리하는 배치 작업 (일시정지/중단/네트워크 장애/연속 실패 대응)."""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Callable

from .compressor import (Compressor, ItemResult, Options, ST_CANCEL, ST_FAIL,
                         ST_NOTRUN, ST_SKIP, ST_SUCCESS)
from .bandizip import BandizipRunner
from .runlog import RunLog, human_size
from .scanner import Candidate, ScanResult


@dataclass
class Summary:
    dirs_scanned: int = 0
    targets: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    cancelled: int = 0
    not_run: int = 0
    created: int = 0
    src_bytes: int = 0
    out_bytes: int = 0
    deleted_originals: int = 0
    stop_reason: str = ""

    def lines(self) -> list[str]:
        out = [
            f"검사한 폴더 수 : {self.dirs_scanned:,}",
            f"압축 대상 수   : {self.targets:,}",
            f"압축 성공      : {self.success:,}",
            f"압축 실패      : {self.failed:,}",
            f"건너뜀         : {self.skipped:,}",
            f"취소/미실행    : {self.cancelled + self.not_run:,}",
            f"생성된 파일 수 : {self.created:,}",
            f"원본 용량(성공분): {human_size(self.src_bytes)}",
            f"결과 파일 용량 : {human_size(self.out_bytes)}",
        ]
        if self.deleted_originals:
            out.append(f"삭제된 원본 폴더: {self.deleted_originals:,}")
        if self.stop_reason:
            out.append(f"중단 사유      : {self.stop_reason}")
        return out


@dataclass
class Callbacks:
    log: Callable[[str], None] = lambda s: None
    item_started: Callable[[int, Candidate], None] = lambda i, c: None
    item_finished: Callable[[int, ItemResult], None] = lambda i, r: None
    overall: Callable[[int, int], None] = lambda d, t: None
    file_progress: Callable[[int, int], None] = lambda d, t: None
    paused: Callable[[bool], None] = lambda p: None


class BatchJob:
    def __init__(self, root: str, items: list[Candidate], runner: BandizipRunner, options: Options,
                 runlog: RunLog, cb: Callbacks, scan: ScanResult | None = None):
        self.root = root
        self.items = items
        self.runner = runner
        self.opt = options
        self.runlog = runlog
        self.cb = cb
        self.scan = scan
        self.cancel_event = threading.Event()
        self.resume_event = threading.Event()
        self.resume_event.set()
        self.summary = Summary(dirs_scanned=scan.dirs_scanned if scan else 0, targets=len(items))

    # 제어 ---------------------------------------------------------------
    def pause(self):
        self.resume_event.clear()

    def resume(self):
        self.resume_event.set()

    def cancel(self):
        self.cancel_event.set()
        self.resume_event.set()

    @property
    def is_paused(self) -> bool:
        return not self.resume_event.is_set()

    # --------------------------------------------------------------------
    def _log(self, s: str):
        self.runlog.line(s)
        self.cb.log(s)

    def _wait_root_available(self) -> bool:
        for attempt in range(6):
            if os.path.isdir(self.root):
                return True
            self._log(f"⚠ 작업 폴더에 접근할 수 없음 (NAS 연결 확인 중 {attempt + 1}/6)…")
            if self.cancel_event.wait(10):
                return False
        return os.path.isdir(self.root)

    def run(self) -> Summary:
        s = self.summary
        comp = Compressor(self.runner, self.opt, self.cancel_event, log=self._log,
                          file_progress=self.cb.file_progress)
        self._log(f"작업 시작: {len(self.items)}개 항목, 압축 레벨 -l:{self.opt.level}, "
                  f"원본 {'검증 후 삭제' if self.opt.delete_originals else '보존'}")
        if self.scan:
            for r in self.scan.records:
                if r.status in ("건너뜀", "오류"):
                    self.runlog.row("검색", r.path, r.status, r.reason)
        consecutive_fail = 0
        last_error = ""
        total = len(self.items)
        for c in self.items:
            c.status = "대기"
        for i, c in enumerate(self.items):
            # 일시정지
            if not self.resume_event.is_set():
                self.cb.paused(True)
                self._log("일시정지됨")
                self.resume_event.wait()
                self.cb.paused(False)
                if not self.cancel_event.is_set():
                    self._log("다시 시작")
            if self.cancel_event.is_set():
                s.stop_reason = s.stop_reason or "사용자 중단"
                break
            if not self._wait_root_available():
                s.stop_reason = "NAS 연결 끊김"
                break

            self.cb.overall(i, total)
            c.status = "진행 중"
            self.cb.item_started(i, c)
            self._log(f"[{i + 1}/{total}] 압축: {c.path}")
            r = comp.process(c)
            self._record(i, r)
            if r.status == ST_FAIL:
                consecutive_fail += 1
                last_error = r.message
                if consecutive_fail >= self.opt.max_consecutive_failures > 0:
                    s.stop_reason = (f"연속 {consecutive_fail}회 실패로 자동 중단 "
                                     f"(마지막 오류: {last_error[:150]})")
                    self._log("⛔ " + s.stop_reason)
                    break
            elif r.status in (ST_SUCCESS, ST_SKIP):
                consecutive_fail = 0
            if r.status == ST_CANCEL:
                s.stop_reason = "사용자 중단"
                break
        # 남은 항목 = 미실행
        if self.cancel_event.is_set() or s.stop_reason:
            for j in range(total):
                c = self.items[j]
                if c.status in ("대기", "진행 중"):
                    r = ItemResult(c, ST_NOTRUN, f"{s.stop_reason or '중단'}으로 실행하지 않음")
                    self._record(j, r, quiet=True)
        self.cb.overall(total, total)
        self._log("작업 종료")
        for line in s.lines():
            self._log("  " + line)
        self._log(f"CSV: {self.runlog.csv_path}")
        return s

    def _record(self, idx: int, r: ItemResult, quiet: bool = False):
        s = self.summary
        c = r.candidate
        c.status = r.status
        if r.status == ST_SUCCESS:
            s.success += 1
            s.created += 1
            s.src_bytes += c.total_bytes
            s.out_bytes += r.output_bytes
            if r.original_action.startswith("삭제"):
                s.deleted_originals += 1
        elif r.status == ST_FAIL:
            s.failed += 1
        elif r.status == ST_SKIP:
            s.skipped += 1
        elif r.status == ST_CANCEL:
            s.cancelled += 1
        elif r.status == ST_NOTRUN:
            s.not_run += 1
        desc = r.message + ((" | " + " | ".join(r.warnings)) if r.warnings else "")
        self.runlog.row("압축", c.path, r.status, desc, c.image_count, c.total_bytes,
                        r.output_path if r.status == ST_SUCCESS else "", r.output_bytes, r.original_action)
        if not quiet:
            mark = {"성공": "✔", "실패": "✖", "건너뜀": "→", "취소": "■"}.get(r.status, "·")
            extra = f" · 원본 {r.original_action}" if r.status == ST_SUCCESS else ""
            self._log(f"  {mark} {r.status}: {r.message}{extra}")
        else:
            self.runlog.line(f"미실행: {c.path}")
        self.cb.item_finished(idx, r)
