"""작업 로그(.log) / 결과(.csv) 기록. 로컬 사용자 데이터 폴더에 저장."""
from __future__ import annotations

import csv
import datetime as _dt
import os
import secrets
import threading

from . import APP_ID

CSV_HEADER = ["시간", "구분", "원본 경로", "상태", "설명", "이미지 수",
              "원본 바이트", "출력 경로", "출력 바이트", "원본 처리"]


def default_log_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    d = os.path.join(base, APP_ID, "logs")
    os.makedirs(d, exist_ok=True)
    return d


def now_str() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def human_size(n: int | float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


class RunLog:
    def __init__(self, log_dir: str | None = None, kind: str = "run"):
        self.dir = log_dir or default_log_dir()
        os.makedirs(self.dir, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        base = f"{stamp}_{kind}_{secrets.token_hex(3)}"
        self.log_path = os.path.join(self.dir, base + ".log")
        self.csv_path = os.path.join(self.dir, base + ".csv")
        self._lock = threading.Lock()
        self._log = open(self.log_path, "a", encoding="utf-8", newline="\n")
        self._csv_f = open(self.csv_path, "w", encoding="utf-8-sig", newline="")
        self._csv = csv.writer(self._csv_f)
        self._csv.writerow(CSV_HEADER)
        self._csv_f.flush()

    def line(self, msg: str) -> None:
        with self._lock:
            self._log.write(f"{now_str()} {msg}\n")
            self._log.flush()

    def row(self, kind: str, path: str, status: str, desc: str, images: int = 0,
            src_bytes: int = 0, out_path: str = "", out_bytes: int = 0, orig: str = "") -> None:
        with self._lock:
            self._csv.writerow([now_str(), kind, path, status, desc, images, src_bytes,
                                out_path, out_bytes, orig])
            self._csv_f.flush()

    def close(self) -> None:
        with self._lock:
            for f in (self._log, self._csv_f):
                try:
                    f.close()
                except OSError:
                    pass
