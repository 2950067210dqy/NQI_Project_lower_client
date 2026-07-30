"""下位机文件夹自动上传与本地上传记录服务。"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal
from loguru import logger

from api.api_client import APIClient
from metadata.meter_data import DataType, MeterDataManager


class UploadHistoryStore:
    """使用 SQLite 保存自动/手动上传状态，应用重启后仍可继续上传。"""

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self):
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS upload_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    data_type TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    modified_ns INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    server_file_id TEXT,
                    message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_attempt_at TEXT,
                    next_retry_at TEXT,
                    UNIQUE(file_path, modified_ns, file_size, source)
                )
                """
            )
            self._migrate_history_unique_key(connection)

            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_upload_history_pending "
                "ON upload_history(status, next_retry_at, created_at)"
            )

    def _migrate_history_unique_key(self, connection):
        """兼容旧记录库：允许同一文件分别保留自动和手动上传记录。"""
        old_unique_key = ["file_path", "modified_ns", "file_size"]
        for index in connection.execute("PRAGMA index_list('upload_history')").fetchall():
            if not index[2]:
                continue
            columns = [item[2] for item in connection.execute(f"PRAGMA index_info('{index[1]}')").fetchall()]
            if columns != old_unique_key:
                continue
            connection.execute(
                """
                CREATE TABLE upload_history_migrating (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    data_type TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    modified_ns INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    server_file_id TEXT,
                    message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_attempt_at TEXT,
                    next_retry_at TEXT,
                    UNIQUE(file_path, modified_ns, file_size, source)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO upload_history_migrating (
                    id, file_path, file_name, data_type, file_size, modified_ns, source,
                    status, retry_count, server_file_id, message, created_at, updated_at,
                    last_attempt_at, next_retry_at
                ) SELECT id, file_path, file_name, data_type, file_size, modified_ns, source,
                    status, retry_count, server_file_id, message, created_at, updated_at,
                    last_attempt_at, next_retry_at
                FROM upload_history
                """
            )
            connection.execute("DROP TABLE upload_history")
            connection.execute("ALTER TABLE upload_history_migrating RENAME TO upload_history")
            logger.info("已迁移上传记录库：自动上传和手动上传可保留同一文件的独立记录")
            break

    def delete_records(self, record_ids: list[int]) -> int:
        """仅删除本地上传记录，不删除源文件或服务器端数据。"""
        valid_ids = [int(record_id) for record_id in record_ids if str(record_id).isdigit()]
        if not valid_ids:
            return 0
        placeholders = ",".join("?" for _ in valid_ids)
        with self._connect() as connection:
            cursor = connection.execute(f"DELETE FROM upload_history WHERE id IN ({placeholders})", valid_ids)
            return cursor.rowcount
    @staticmethod
    def _file_identity(file_path: Path) -> tuple[str, str, int, int]:
        stat = file_path.stat()
        return str(file_path.resolve()), file_path.name, int(stat.st_size), int(stat.st_mtime_ns)

    def recover_interrupted_auto_uploads(self) -> int:
        """把上次异常退出时遗留的“上传中”记录恢复为可续传队列。"""
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE upload_history
                SET status = '等待上传', message = '检测到上次上传中断，已恢复到自动上传队列',
                    updated_at = ?, next_retry_at = ?
                WHERE source = '自动监听' AND status = '上传中'
                """,
                (now, now),
            )
            return cursor.rowcount

    def register_auto_file(self, file_path: Path, data_type: DataType) -> bool:
        """登记稳定文件；中断或重启后优先复用已有自动任务而不是重复建队。"""
        path_text, file_name, file_size, modified_ns = self._file_identity(file_path)
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT * FROM upload_history
                WHERE source = '自动监听' AND file_path = ?
                ORDER BY id DESC LIMIT 1
                """,
                (path_text,),
            ).fetchone()
            if existing is not None:
                same_version = (
                    int(existing["file_size"]) == file_size
                    and int(existing["modified_ns"]) == modified_ns
                )
                if same_version:
                    # 同一版本已有记录时保留其状态；等待或重试任务由后续领取逻辑继续上传。
                    return False
                if existing["status"] in ("等待上传", "等待重试", "上传中"):
                    # 文件仍在队列中但内容发生变化，复用原记录并以最新文件版本重新开始。
                    connection.execute(
                        """
                        UPDATE upload_history
                        SET file_name = ?, data_type = ?, file_size = ?, modified_ns = ?,
                            status = '等待上传', retry_count = 0, server_file_id = NULL,
                            message = '检测到排队文件更新，已使用最新版本重新排队',
                            updated_at = ?, last_attempt_at = NULL, next_retry_at = ?
                        WHERE id = ?
                        """,
                        (file_name, data_type.value, file_size, modified_ns, now, now, existing["id"]),
                    )
                    return True

            cursor = connection.execute(
                """
                INSERT INTO upload_history (
                    file_path, file_name, data_type, file_size, modified_ns, source,
                    status, created_at, updated_at, next_retry_at
                ) VALUES (?, ?, ?, ?, ?, '自动监听', '等待上传', ?, ?, ?)
                """,
                (path_text, file_name, data_type.value, file_size, modified_ns, now, now, now),
            )
            return cursor.rowcount > 0
    def claim_next_auto_upload(self) -> Optional[dict]:
        """领取一条到期待上传记录，避免重复上传同一个文件。"""
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM upload_history
                WHERE source = '自动监听' AND status IN ('等待上传', '等待重试')
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY created_at ASC, id ASC LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE upload_history
                SET status = '上传中', last_attempt_at = ?, updated_at = ?, message = '正在自动上传'
                WHERE id = ?
                """,
                (now, now, row["id"]),
            )
            return dict(row)

    def mark_auto_success(self, record_id: int, result: dict):
        now = datetime.now().isoformat(timespec="seconds")
        server_file_id = result.get("file_id") or result.get("id")
        message = result.get("message") or "自动上传成功"
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE upload_history
                SET status = '上传成功', server_file_id = ?, message = ?, updated_at = ?,
                    next_retry_at = NULL
                WHERE id = ?
                """,
                (str(server_file_id) if server_file_id is not None else None, message, now, record_id),
            )

    def mark_auto_failure(self, record_id: int, error: str, missing_file: bool = False):
        """失败后指数退避，服务器宕机时不会持续占用网络和线程。"""
        now = datetime.now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT retry_count FROM upload_history WHERE id = ?", (record_id,)
            ).fetchone()
            retries = int(row["retry_count"] if row else 0) + 1
            if missing_file:
                status = "文件不存在"
                next_retry = None
            else:
                # 5、10、20…秒，最长 5 分钟；网络恢复后会自动续传。
                delay_seconds = min(300, 5 * (2 ** min(retries - 1, 6)))
                status = "等待重试"
                next_retry = (now + timedelta(seconds=delay_seconds)).isoformat(timespec="seconds")
            connection.execute(
                """
                UPDATE upload_history
                SET status = ?, retry_count = ?, message = ?, updated_at = ?, next_retry_at = ?
                WHERE id = ?
                """,
                (status, retries, str(error)[:1000], now.isoformat(timespec="seconds"), next_retry, record_id),
            )

    def record_manual_result(self, file_path: Path, data_type: DataType, success: bool, message: str):
        """把手动上传结果也写入同一张本地历史表。"""
        try:
            path_text, file_name, file_size, modified_ns = self._file_identity(file_path)
        except OSError:
            return
        now = datetime.now().isoformat(timespec="seconds")
        status = "上传成功" if success else "上传失败"
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO upload_history (
                    file_path, file_name, data_type, file_size, modified_ns, source,
                    status, message, created_at, updated_at, last_attempt_at
                ) VALUES (?, ?, ?, ?, ?, '手动上传', ?, ?, ?, ?, ?)
                ON CONFLICT(file_path, modified_ns, file_size, source) DO UPDATE SET
                    source = '手动上传', status = excluded.status, message = excluded.message,
                    updated_at = excluded.updated_at, last_attempt_at = excluded.last_attempt_at
                """,
                (path_text, file_name, data_type.value, file_size, modified_ns, status, message[:1000], now, now, now),
            )


    def list_auto_recent(self, limit: int = 300) -> list[dict]:
        """读取自动监听记录，供下位机数据上传页在启动后回放状态。"""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM upload_history
                WHERE source = '自动监听'
                ORDER BY updated_at DESC, id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_recent(self, limit: int = 300) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM upload_history ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]


class FolderAutoUploadThread(QThread):
    """监听两个配置目录，并在已连接服务器时串行自动上传待上传文件。"""

    status_changed = pyqtSignal(str)
    file_discovered = pyqtSignal(str, str)
    upload_started = pyqtSignal(str)
    upload_result = pyqtSignal(dict)
    history_changed = pyqtSignal()

    def __init__(self, history_store: UploadHistoryStore, parent=None):
        super().__init__(parent)
        self.history_store = history_store
        self._stop_event = threading.Event()
        self._settings_lock = threading.Lock()
        self._last_status = ""
        self._stable_files: dict[str, tuple[int, int]] = {}
        self._settings = {
            "enabled": False,
            "excel_dir": "",
            "image_dir": "",
            "scan_interval": 3,
            "request_timeout": 10,
            "connected": False,
            "server_url": "",
            "device_id": "",
            "hardware_key": "",
            "location": "",
        }

    def update_settings(self, **settings):
        """由主线程更新配置；锁保证监听线程读取到完整快照。"""
        with self._settings_lock:
            self._settings.update(settings)

    def stop(self):
        self._stop_event.set()

    def _snapshot_settings(self) -> dict:
        with self._settings_lock:
            return dict(self._settings)

    def _emit_status(self, text: str):
        if text != self._last_status:
            self._last_status = text
            self.status_changed.emit(text)

    def _discover_files(self, settings: dict):
        watched_directories = (
            (settings.get("excel_dir"), DataType.EXCEL),
            (settings.get("image_dir"), DataType.IMAGE),
        )
        for directory_text, data_type in watched_directories:
            if not directory_text:
                continue
            directory = Path(directory_text).expanduser()
            if not directory.is_dir():
                self._emit_status(f"监听目录不可用: {directory}")
                continue
            for file_path in directory.iterdir():
                if not file_path.is_file() or MeterDataManager.detect_data_type(file_path) != data_type:
                    continue
                try:
                    file_state = (int(file_path.stat().st_size), int(file_path.stat().st_mtime_ns))
                except OSError:
                    continue
                path_text = str(file_path.resolve())
                # 连续两轮扫描状态一致，认为文件写入已完成，避免上传半写入文件。
                if self._stable_files.get(path_text) != file_state:
                    self._stable_files[path_text] = file_state
                    continue
                try:
                    if self.history_store.register_auto_file(file_path, data_type):
                        self.file_discovered.emit(path_text, data_type.value)
                        self.history_changed.emit()
                except Exception as exc:
                    logger.exception(f"自动上传登记失败: {file_path}: {exc}")

    def _upload_one_pending_file(self, settings: dict):
        if not settings.get("connected"):
            self._emit_status("监听中，等待服务器连接后自动上传")
            return
        if not all(settings.get(key) for key in ("server_url", "device_id", "hardware_key")):
            self._emit_status("监听中，等待完整服务器和设备配置")
            return

        record = self.history_store.claim_next_auto_upload()
        if record is None:
            self._emit_status("自动监听中，暂无待上传文件")
            return
        self.history_changed.emit()
        file_path = Path(record["file_path"])
        try:
            if not file_path.is_file():
                raise FileNotFoundError("源文件已不存在")
            self._emit_status(f"正在自动上传: {file_path.name}")
            # 先通知界面更新行状态，再进行可能耗时的 HTTP 文件上传。
            self.upload_started.emit(str(file_path))
            client = APIClient(settings["server_url"], timeout=int(settings["request_timeout"]))
            result = client.upload_file(
                settings["device_id"],
                settings["hardware_key"],
                file_path,
                description=f"自动监听上传 - {file_path.stem}",
                location=settings.get("location") or None,
            )
            self.history_store.mark_auto_success(record["id"], result)
            self.upload_result.emit({
                "success": True,
                "file_name": file_path.name,
                "file_path": str(file_path),
                "data_type": record["data_type"],
                "message": result.get("message") or "自动上传成功",
            })
        except Exception as exc:
            missing_file = isinstance(exc, FileNotFoundError)
            self.history_store.mark_auto_failure(record["id"], str(exc), missing_file=missing_file)
            self.upload_result.emit({
                "success": False,
                "file_name": file_path.name,
                "file_path": str(file_path),
                "data_type": record["data_type"],
                "message": str(exc),
            })
            logger.warning(f"自动上传失败，已进入退避重试: {file_path}: {exc}")
        finally:
            self.history_changed.emit()

    def run(self):
        self._emit_status("自动监听线程已启动")
        while not self._stop_event.is_set():
            try:
                settings = self._snapshot_settings()
                if settings.get("enabled"):
                    self._discover_files(settings)
                    self._upload_one_pending_file(settings)
                else:
                    self._emit_status("自动监听未启用")
                self._stop_event.wait(max(1, int(settings.get("scan_interval", 3))))
            except Exception as exc:
                logger.exception(f"自动上传监听线程异常: {exc}")
                self._emit_status(f"自动监听异常: {exc}")
                self._stop_event.wait(5)
        self._emit_status("自动监听线程已停止")
