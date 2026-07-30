import sys
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QLabel, QLineEdit,
                             QTextEdit, QFileDialog, QMessageBox, QGroupBox,
                             QProgressBar, QListWidget, QListWidgetItem, QSplitter,
                             QTabWidget, QFrame, QRadioButton, QButtonGroup, QCheckBox,
                             QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QThreadPool, QRunnable, pyqtSlot, QTimer, QEvent
from PyQt6.QtGui import QFont, QColor, QIcon, QPixmap, QGuiApplication
from pathlib import Path
from loguru import logger
import socket
import requests
from datetime import datetime

from config.config import lower_config
from security.hardware_key import hardware_key_generator
from api.api_client import APIClient
from api.long_polling_client import LowerLongPollingThread
from metadata.meter_data import MeterDataManager, DataType, MeterData
from auto_upload_service import FolderAutoUploadThread, UploadHistoryStore

# 配置日志
logger.add("logs/lower_{time}.log", rotation="10 MB", retention="30 days", encoding='utf-8')


class UploadWorker(QRunnable):
    """文件上传工作线程"""

    class Signals(QThread):
        progress = pyqtSignal(str, int)  # 文件名, 进度
        finished = pyqtSignal(str, bool, str)  # 文件名, 成功, 消息

    def __init__(self, client: APIClient, device_id: str, hardware_key: str,
                 meter_data: MeterData, location: str = None):
        super().__init__()
        self.signals = UploadWorker.Signals()
        self.client = client
        self.device_id = device_id
        self.hardware_key = hardware_key
        self.meter_data = meter_data
        self.location = location

    @pyqtSlot()
    def run(self):
        """执行上传"""
        file_name = self.meter_data.file_path.name
        try:
            self.signals.progress.emit(file_name, 30)

            result = self.client.upload_file(
                self.device_id,
                self.hardware_key,
                self.meter_data.file_path,
                self.meter_data.description,
                location=self.location
            )

            self.signals.progress.emit(file_name, 100)

            original_size = result.get('original_size', 0)
            compressed_size = result.get('compressed_size', 0)
            compression_ratio = ((original_size - compressed_size) / original_size * 100) if original_size > 0 else 0

            message = f"上传成功！文件ID: {result.get('file_id')}\n"

            # 对于图片添加压缩信息
            if self.meter_data.is_image and compression_ratio > 0:
                size_mb_before = original_size / (1024 * 1024)
                size_mb_after = compressed_size / (1024 * 1024)
                message += f"原始大小: {size_mb_before:.2f}MB\n"
                message += f"压缩后: {size_mb_after:.2f}MB\n"
                message += f"压缩率: {compression_ratio:.1f}%"

            self.signals.finished.emit(file_name, True, message)

        except Exception as e:
            self.signals.progress.emit(file_name, 0)
            self.signals.finished.emit(file_name, False, f"上传失败: {str(e)}")


class AsyncTaskThread(QThread):
    """通用后台任务线程，用于把网络请求从主界面线程中拆出去。"""

    result_ready = pyqtSignal(object)
    error_occurred = pyqtSignal(str)

    def __init__(self, target, parent=None):
        super().__init__(parent)
        self.target = target

    def run(self):
        try:
            self.result_ready.emit(self.target())
        except Exception as exc:
            logger.exception(f"后台任务执行失败: {exc}")
            self.error_occurred.emit(str(exc))


class LoadingOverlay(QWidget):
    """下位机统一 loading 遮罩。"""

    def __init__(self, parent=None, text="处理中..."):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(
            "QWidget { background-color: rgba(0, 0, 0, 135); }"
            "QLabel { color: white; font-size: 15px; font-weight: bold; }"
            "QProgressBar { border: 1px solid white; border-radius: 6px; text-align: center; background: rgba(255,255,255,40); }"
            "QProgressBar::chunk { background-color: #4CAF50; border-radius: 5px; }"
        )

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label = QLabel(text)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedWidth(240)
        layout.addWidget(self.label, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.progress, alignment=Qt.AlignmentFlag.AlignCenter)

    def update_text(self, text: str):
        self.label.setText(text)

    def resize_to_parent(self):
        if self.parent() is not None:
            self.setGeometry(self.parent().rect())


class MeterDataListItem(QFrame):
    """三相表数据列表项"""

    def __init__(self, meter_data: MeterData, upload_source: str = "手动上传", parent=None):
        super().__init__(parent)
        self.meter_data = meter_data
        self.upload_source = upload_source
        self.uploaded = False  # 是否已上传
        self.setFrameStyle(QFrame.Shape.StyledPanel | QFrame.Shadow.Raised)
        self.init_ui()

    def init_ui(self):
        """初始化UI"""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 5, 8, 5)

        # 复选框
        self.checkbox = QCheckBox()
        self.checkbox.setChecked(True)  # 默认选中
        layout.addWidget(self.checkbox, stretch=0)

        # 数据类型图标和名称
        type_label = QLabel()
        if self.meter_data.is_excel:
            type_label.setText("📊 电量数据")
            type_label.setStyleSheet("color: #0066cc; font-weight: bold;")
        else:
            type_label.setText("📷 几何量数据")
            type_label.setStyleSheet("color: #cc6600; font-weight: bold;")
        layout.addWidget(type_label, stretch=1)

        # 来源列：自动监听与手动加入的同名文件也能在列表中明确区分。
        self.source_label = QLabel(f"来源：{self.upload_source}")
        self.source_label.setStyleSheet("color: #7a4f00;" if self.upload_source == "自动上传" else "color: #555555;")
        layout.addWidget(self.source_label, stretch=0)

        # 文件名
        self.name_label = QLabel(self.meter_data.file_path.name)
        self.name_label.setToolTip(str(self.meter_data.file_path))
        layout.addWidget(self.name_label, stretch=2)

        # 文件大小
        size_kb = self.meter_data.file_size / 1024
        if size_kb < 1024:
            size_text = f"{size_kb:.1f} KB"
        else:
            size_text = f"{size_kb / 1024:.2f} MB"
        self.size_label = QLabel(size_text)
        self.size_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.size_label, stretch=0)

        # 描述
        self.desc_label = QLabel(self.meter_data.description)
        self.desc_label.setStyleSheet("color: gray; font-size: 10px;")
        layout.addWidget(self.desc_label, stretch=1)

        # 状态
        self.status_label = QLabel("等待上传")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("color: gray;")
        layout.addWidget(self.status_label, stretch=0)

        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximumHeight(15)
        self.progress_bar.setMaximumWidth(100)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar, stretch=0)

        if self.upload_source == "自动上传":
            # 自动上传由后台监听线程管理，不能被手动批量上传按钮误选。
            self.checkbox.setChecked(False)
            self.checkbox.setEnabled(False)
            self.checkbox.setToolTip("该文件由自动监听任务上传")

    def set_status(self, status: str, color: str = "black"):
        """设置状态"""
        self.status_label.setText(status)
        self.status_label.setStyleSheet(f"color: {color};")
    
    def set_uploaded(self, uploaded: bool = True):
        """设置上传状态，并阻止成功文件被重复选择上传。"""
        self.uploaded = uploaded
        if uploaded:
            self.checkbox.setChecked(False)
            self.checkbox.setEnabled(False)
            tip = "该文件已上传成功，不能重复选择上传"
            # 禁用后的复选框仍保留悬停提示，便于用户理解不能勾选的原因。
            self.checkbox.setToolTip(tip)
            self.setToolTip(tip)
        else:
            # 上传失败或状态被重置时允许用户重新选择上传。
            self.checkbox.setEnabled(True)
            self.checkbox.setToolTip("")
            self.setToolTip("")
    
    def is_selected(self) -> bool:
        """是否被选中"""
        return self.checkbox.isChecked() and not self.uploaded

    def set_progress(self, value: int):
        """设置进度"""
        if value > 0:
            self.progress_bar.setVisible(True)
            self.progress_bar.setValue(value)
        else:
            self.progress_bar.setVisible(False)


class LowerComputerWindow(QMainWindow):
    """下位机主窗口 - 三相表数据上传"""

    connected_signal = pyqtSignal()
    disconnected_signal = pyqtSignal()
    error_signal = pyqtSignal(str)
    registration_pending_signal = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.client = None
        self.long_polling_thread = None  # HTTP长轮询心跳线程
        self.authenticated = False
        # 自动上传记录独立保存在 exe/源码运行目录，程序重启后仍可查看和续传。
        self.upload_history_store = UploadHistoryStore(lower_config.app_dir / "upload_history.sqlite3")
        self.auto_upload_thread = None
        # 历史表只在可见时节流刷新，避免后台上传期间频繁重绘造成卡顿。
        self.upload_history_row_ids = []
        self.upload_history_refresh_scheduled = False
        self.upload_history_dirty = False
        # 用户滚动记录表时暂停重绘，避免后台上传状态刷新抢占界面线程。
        self.upload_history_interacting = False
        self.upload_history_interaction_timer = QTimer(self)
        self.upload_history_interaction_timer.setSingleShot(True)
        self.upload_history_interaction_timer.timeout.connect(self._resume_upload_history_refresh)
        self.tab_switch_loading_token = 0
        self.data_items = {}  # 手动上传：文件路径 -> MeterDataListItem
        self.auto_data_items = {}  # 自动上传：文件路径 -> MeterDataListItem
        self.meter_data_manager = MeterDataManager(lower_config.cache_dir)
        self.thread_pool = QThreadPool()
        self.thread_pool.setMaxThreadCount(lower_config.concurrent_uploads)
        self.registration_status_timer = QTimer(self)
        self.registration_status_timer.setInterval(10000)
        self.registration_status_timer.timeout.connect(lambda: self.query_registration_status(silent=True, show_dialog=True))
        self.last_registration_status = None
        self.last_registration_request_id = None
        self.loading_overlay = None
        self.active_async_threads = []
        self.pending_upload_count = 0
        self.init_ui()
        self.load_config()
        self.connected_signal.connect(self.on_connected)
        self.disconnected_signal.connect(self.on_disconnected)
        self.error_signal.connect(self.on_error)
        self.registration_pending_signal.connect(self.on_registration_pending)
        self._start_auto_upload_service()
        QTimer.singleShot(500, self.auto_detect_location)

    def init_ui(self):
        """初始化界面"""
        self.setWindowTitle("三相表数据上传系统")
        self.setGeometry(100, 100, 1000, 750)
        self.setWindowIcon(QIcon(lower_config.ui_NQI_path))
        # 中央部件
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # 主布局
        main_layout = QVBoxLayout(central_widget)
        top_layout = QHBoxLayout()

        # NQI图片标识
        nqi_image = QLabel()
        nqi_image.setPixmap(QPixmap(lower_config.ui_NQI_path))
        nqi_image.setAlignment(Qt.AlignmentFlag.AlignLeft)
        top_layout.addWidget(nqi_image)
        # 标题
        title_label = QLabel("三相表电量及几何量数据上传系统")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_font = QFont()
        title_font.setPointSize(16)
        title_font.setBold(True)
        title_label.setFont(title_font)
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top_layout.addWidget(title_label)
        # 学校标识
        school_image = QLabel()
        school_image.setPixmap(QPixmap(lower_config.ui_school_path))
        school_image.setAlignment(Qt.AlignmentFlag.AlignRight)
        top_layout.addWidget(school_image)

        main_layout.addLayout(top_layout)

        # 配置区域
        config_group = QGroupBox("设备配置")
        config_layout = QVBoxLayout()

        # 第一行：服务器和设备ID
        row1_layout = QHBoxLayout()
        row1_layout.addWidget(QLabel("服务器地址:"))
        self.server_input = QLineEdit()
        self.server_input.setPlaceholderText("http://localhost:8000")
        row1_layout.addWidget(self.server_input)

        row1_layout.addWidget(QLabel("设备ID:"))
        self.device_id_input = QLineEdit()
        self.device_id_input.setPlaceholderText("请输入唯一设备ID")
        row1_layout.addWidget(self.device_id_input)
        config_layout.addLayout(row1_layout)

        # 第二行：设备名称和硬件密钥
        row2_layout = QHBoxLayout()
        row2_layout.addWidget(QLabel("设备名称:"))
        self.device_name_input = QLineEdit()
        row2_layout.addWidget(self.device_name_input)

        row2_layout.addWidget(QLabel("所在城市:"))
        self.location_input = QLineEdit()
        self.location_input.setPlaceholderText("例如：长沙市")
        row2_layout.addWidget(self.location_input)

        row2_layout.addWidget(QLabel("硬件密钥:"))
        self.hardware_key_label = QLabel("未生成")
        self.hardware_key_label.setStyleSheet("color: blue;")
        row2_layout.addWidget(self.hardware_key_label)

        self.generate_key_btn = QPushButton("生成密钥")
        self.generate_key_btn.clicked.connect(self.generate_hardware_key)
        row2_layout.addWidget(self.generate_key_btn)
        config_layout.addLayout(row2_layout)

        # 第三行：连接按钮
        button_layout = QHBoxLayout()
        self.register_btn = QPushButton("注册设备")
        self.register_btn.clicked.connect(self.register_device)
        button_layout.addWidget(self.register_btn)

        self.query_status_btn = QPushButton("查看注册状态")
        self.query_status_btn.clicked.connect(self.query_registration_status)
        button_layout.addWidget(self.query_status_btn)

        self.connect_btn = QPushButton("连接服务器")
        self.connect_btn.clicked.connect(self.connect_to_server)
        button_layout.addWidget(self.connect_btn)

        self.save_config_btn = QPushButton("保存配置")
        self.save_config_btn.clicked.connect(self.save_current_config)
        button_layout.addWidget(self.save_config_btn)

        button_layout.addStretch()
        config_layout.addLayout(button_layout)

        status_layout = QHBoxLayout()
        status_layout.addWidget(QLabel("注册状态:"))
        self.registration_status_label = QLabel("未查询")
        self.registration_status_label.setStyleSheet("color: gray; font-weight: bold;")
        status_layout.addWidget(self.registration_status_label)
        status_layout.addWidget(QLabel("审批进度:"))
        self.registration_detail_label = QLabel("请先提交注册申请")
        self.registration_detail_label.setWordWrap(True)
        self.registration_detail_label.setStyleSheet("color: #666666;")
        status_layout.addWidget(self.registration_detail_label, stretch=1)
        config_layout.addLayout(status_layout)

        config_group.setLayout(config_layout)
        main_layout.addWidget(config_group)
        # 分割器
        splitter = QSplitter(Qt.Orientation.Vertical)

        # 数据上传区域
        upload_group = QGroupBox("数据上传")
        upload_layout = QVBoxLayout()

        # 工具栏 - 分为两部分
        toolbar = QHBoxLayout()

        # 电量数据操作
        excel_frame = QGroupBox("电量数据 (Excel)")
        excel_layout = QHBoxLayout()
        self.add_excel_btn = QPushButton("添加电量数据")
        self.add_excel_btn.clicked.connect(self.add_excel_files)
        self.add_excel_btn.setEnabled(False)
        self.add_excel_btn.setStyleSheet("background-color: #e8f4f8;")
        excel_layout.addWidget(self.add_excel_btn)
        excel_frame.setLayout(excel_layout)
        toolbar.addWidget(excel_frame)

        # 几何量数据操作
        image_frame = QGroupBox("几何量数据 (图片)")
        image_layout = QHBoxLayout()
        self.add_image_btn = QPushButton("添加几何量数据")
        self.add_image_btn.clicked.connect(self.add_image_files)
        self.add_image_btn.setEnabled(False)
        self.add_image_btn.setStyleSheet("background-color: #f8e8f4;")
        image_layout.addWidget(self.add_image_btn)
        image_frame.setLayout(image_layout)
        toolbar.addWidget(image_frame)

        upload_layout.addLayout(toolbar)

        # 自动文件夹上传配置：监听和网络请求都在后台线程，不阻塞 PyQt 主界面。
        auto_upload_group = QGroupBox("自动文件夹上传")
        auto_upload_layout = QVBoxLayout()
        auto_upload_head = QHBoxLayout()
        self.auto_upload_enabled_checkbox = QCheckBox("启用自动监听")
        auto_upload_head.addWidget(self.auto_upload_enabled_checkbox)
        self.auto_upload_status_label = QLabel("自动监听未启用")
        self.auto_upload_status_label.setStyleSheet("color: #666666;")
        auto_upload_head.addWidget(self.auto_upload_status_label, stretch=1)
        self.save_auto_upload_btn = QPushButton("保存监听设置")
        self.save_auto_upload_btn.clicked.connect(self.save_auto_upload_settings)
        auto_upload_head.addWidget(self.save_auto_upload_btn)
        auto_upload_layout.addLayout(auto_upload_head)

        excel_watch_layout = QHBoxLayout()
        excel_watch_layout.addWidget(QLabel("电量 Excel 文件夹:"))
        self.auto_excel_directory_input = QLineEdit()
        self.auto_excel_directory_input.setPlaceholderText("选择仅包含 .xlsx/.xls 文件的监听文件夹")
        excel_watch_layout.addWidget(self.auto_excel_directory_input, stretch=1)
        self.choose_auto_excel_dir_btn = QPushButton("选择文件夹")
        self.choose_auto_excel_dir_btn.clicked.connect(self.choose_auto_excel_directory)
        excel_watch_layout.addWidget(self.choose_auto_excel_dir_btn)
        auto_upload_layout.addLayout(excel_watch_layout)

        image_watch_layout = QHBoxLayout()
        image_watch_layout.addWidget(QLabel("几何量图片文件夹:"))
        self.auto_image_directory_input = QLineEdit()
        self.auto_image_directory_input.setPlaceholderText("选择仅包含 jpg/jpeg/png/bmp 文件的监听文件夹")
        image_watch_layout.addWidget(self.auto_image_directory_input, stretch=1)
        self.choose_auto_image_dir_btn = QPushButton("选择文件夹")
        self.choose_auto_image_dir_btn.clicked.connect(self.choose_auto_image_directory)
        image_watch_layout.addWidget(self.choose_auto_image_dir_btn)
        auto_upload_layout.addLayout(image_watch_layout)

        auto_upload_tip = QLabel("提示：新增或修改监听文件夹、启用状态等设置后，必须点击“保存监听设置”按钮才会生效。文件连续两次扫描保持不变后才会自动上传；断线或服务器不可用时会记录为等待重试。")
        auto_upload_tip.setStyleSheet("color: #8a6d00;")
        auto_upload_tip.setWordWrap(True)
        auto_upload_layout.addWidget(auto_upload_tip)
        auto_upload_group.setLayout(auto_upload_layout)
        upload_layout.addWidget(auto_upload_group)
        # 数据列表
        list_label = QLabel("数据列表:")
        upload_layout.addWidget(list_label)

        self.data_list_widget = QListWidget()
        self.data_list_widget.setMinimumHeight(200)
        upload_layout.addWidget(self.data_list_widget)

        # 列表操作按钮
        list_btn_layout = QHBoxLayout()
        
        self.select_all_btn = QPushButton("全选")
        self.select_all_btn.clicked.connect(self.select_all_data)
        self.select_all_btn.setEnabled(False)
        list_btn_layout.addWidget(self.select_all_btn)
        
        self.deselect_all_btn = QPushButton("全不选")
        self.deselect_all_btn.clicked.connect(self.deselect_all_data)
        self.deselect_all_btn.setEnabled(False)
        list_btn_layout.addWidget(self.deselect_all_btn)

        self.remove_file_btn = QPushButton("移除选中")
        self.remove_file_btn.clicked.connect(self.remove_selected_data)
        self.remove_file_btn.setEnabled(False)
        list_btn_layout.addWidget(self.remove_file_btn)

        self.clear_files_btn = QPushButton("清空列表")
        self.clear_files_btn.clicked.connect(self.clear_data_list)
        self.clear_files_btn.setEnabled(False)
        list_btn_layout.addWidget(self.clear_files_btn)

        list_btn_layout.addStretch()

        # 统计信息
        self.excel_count_label = QLabel("电量数据: 0")
        self.excel_count_label.setStyleSheet("color: #0066cc; font-weight: bold;")
        list_btn_layout.addWidget(self.excel_count_label)

        self.image_count_label = QLabel("几何量数据: 0")
        self.image_count_label.setStyleSheet("color: #cc6600; font-weight: bold;")
        list_btn_layout.addWidget(self.image_count_label)

        upload_layout.addLayout(list_btn_layout)

        # 上传操作
        upload_action_layout = QHBoxLayout()

        self.upload_btn = QPushButton("开始上传")
        self.upload_btn.setMinimumHeight(40)

        self.upload_btn.clicked.connect(self.upload_data)
        self.upload_btn.setToolTip("仅控制手动勾选的文件上传，对自动监听上传无效")
        self.upload_btn.setEnabled(False)
        self.upload_btn.setStyleSheet("background-color: #4CAF50; color: white;")
        upload_action_layout.addWidget(self.upload_btn)

        self.stop_upload_btn = QPushButton("停止上传")
        self.stop_upload_btn.setMinimumHeight(40)
        self.stop_upload_btn.clicked.connect(self.stop_upload)
        self.stop_upload_btn.setToolTip("仅停止未开始的手动上传任务，对自动监听上传无效")
        self.stop_upload_btn.setEnabled(False)
        self.stop_upload_btn.setStyleSheet("background-color: #f44336; color: white;")
        upload_action_layout.addWidget(self.stop_upload_btn)

        upload_layout.addLayout(upload_action_layout)
        manual_upload_tip = QLabel("提示：开始上传和停止上传按钮仅控制手动上传任务，对自动文件夹监听上传无效。")
        manual_upload_tip.setStyleSheet("color: #8a6d00;")
        manual_upload_tip.setWordWrap(True)
        upload_layout.addWidget(manual_upload_tip)

        upload_group.setLayout(upload_layout)
        splitter.addWidget(upload_group)

        # # 缓存管理区域
        # cache_group = QGroupBox("本地缓存管理")
        # cache_layout = QVBoxLayout()
        #
        # cache_info_layout = QHBoxLayout()
        # self.cache_info_label = QLabel("缓存信息: 加载中...")
        # cache_info_layout.addWidget(self.cache_info_label)
        # cache_info_layout.addStretch()
        #
        # self.refresh_cache_btn = QPushButton("刷新缓存信息")
        # self.refresh_cache_btn.clicked.connect(self.refresh_cache_info)
        # cache_info_layout.addWidget(self.refresh_cache_btn)
        #
        # self.clear_cache_btn = QPushButton("清除缓存")
        # self.clear_cache_btn.clicked.connect(self.clear_cache)
        # cache_info_layout.addWidget(self.clear_cache_btn)
        #
        # cache_layout.addLayout(cache_info_layout)
        # cache_group.setLayout(cache_layout)
        # splitter.addWidget(cache_group)

        # 日志区域
        log_group = QGroupBox("运行日志")
        log_layout = QVBoxLayout()

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        log_layout.addWidget(self.log_text)

        log_group.setLayout(log_layout)
        splitter.addWidget(log_group)

        # 设置分割器比例
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 1)

        # 用标签页分隔上传操作与本地上传记录，记录页不会影响上传页交互。
        self.main_tabs = QTabWidget()
        self.main_tabs.addTab(splitter, "数据上传")
        self.main_tabs.addTab(self._create_upload_history_page(), "上传记录")
        self.main_tabs.currentChanged.connect(self.on_main_tab_changed)
        main_layout.addWidget(self.main_tabs)
        # 状态栏
        self.statusBar().showMessage("未连接")

        # # 刷新缓存信息
        # self.refresh_cache_info()

    def _create_upload_history_page(self):
        """创建本地下位机上传历史页面。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        toolbar = QHBoxLayout()
        self.upload_history_summary_label = QLabel("正在读取本地 SQLite 上传记录...")
        toolbar.addWidget(self.upload_history_summary_label, stretch=1)
        self.refresh_upload_history_btn = QPushButton("刷新记录")
        self.refresh_upload_history_btn.clicked.connect(self.refresh_upload_history)
        toolbar.addWidget(self.refresh_upload_history_btn)
        self.delete_upload_history_btn = QPushButton("删除选中记录")
        self.delete_upload_history_btn.clicked.connect(self.delete_selected_upload_history_records)
        toolbar.addWidget(self.delete_upload_history_btn)
        layout.addLayout(toolbar)

        self.upload_history_table = QTableWidget(0, 9)
        self.upload_history_table.setHorizontalHeaderLabels([
            "时间", "来源", "类型", "文件名", "状态", "重试", "服务器文件ID", "文件路径", "结果说明"
        ])
        self.upload_history_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.upload_history_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.upload_history_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.upload_history_table.setAlternatingRowColors(True)
        self.upload_history_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.upload_history_table.horizontalHeader().setStretchLastSection(True)
        self.upload_history_table.cellDoubleClicked.connect(self.show_upload_history_detail)
        # 滚轮、拖动滑块、键盘浏览期间均临时停掉后台表格刷新。
        self.upload_history_table.installEventFilter(self)
        self.upload_history_table.viewport().installEventFilter(self)
        self.upload_history_table.verticalScrollBar().sliderPressed.connect(self.pause_upload_history_refresh)
        self.upload_history_table.verticalScrollBar().sliderMoved.connect(lambda _value: self.pause_upload_history_refresh())
        self.upload_history_table.verticalScrollBar().sliderReleased.connect(self.defer_upload_history_refresh)
        layout.addWidget(self.upload_history_table)
        detail_tip = QLabel("双击记录可查看完整文件路径和上传结果。自动上传失败会按 5、10、20…秒退避重试，最长间隔 5 分钟。")
        detail_tip.setStyleSheet("color: #666666;")
        detail_tip.setWordWrap(True)
        layout.addWidget(detail_tip)
        return page

    def _auto_upload_settings_from_config(self):
        """读取兼容旧版 ini 的自动上传配置；首次保存时会自动创建对应节。"""
        return {
            "enabled": lower_config.get("auto_upload", "enabled", "false").lower() == "true",
            "excel_dir": lower_config.get("auto_upload", "excel_directory", ""),
            "image_dir": lower_config.get("auto_upload", "image_directory", ""),
            "scan_interval": max(1, int(lower_config.get("auto_upload", "scan_interval_seconds", "3"))),
            "request_timeout": max(3, int(lower_config.get("auto_upload", "request_timeout_seconds", "10"))),
            "connected": self.authenticated,
            "server_url": self.server_input.text().strip(),
            "device_id": self.device_id_input.text().strip(),
            "hardware_key": lower_config.hardware_key,
            "location": self.location_input.text().strip(),
        }

    def _start_auto_upload_service(self):
        """启动后台监听线程；线程不访问 Qt 控件，只通过信号返回界面。"""
        if self.auto_upload_thread is not None:
            return
        recovered_count = self.upload_history_store.recover_interrupted_auto_uploads()
        if recovered_count:
            self.log(f"自动上传已恢复 {recovered_count} 条上次中断的任务")
        self.auto_upload_thread = FolderAutoUploadThread(self.upload_history_store, self)
        self.auto_upload_thread.status_changed.connect(self.on_auto_upload_status_changed)
        self.auto_upload_thread.file_discovered.connect(self.on_auto_file_discovered)
        self.auto_upload_thread.upload_started.connect(self.on_auto_upload_started)
        self.auto_upload_thread.upload_result.connect(self.on_auto_upload_result)
        self.auto_upload_thread.history_changed.connect(self.schedule_upload_history_refresh)
        self.auto_upload_thread.update_settings(**self._auto_upload_settings_from_config())
        self.auto_upload_thread.start()
        # 启动时从 SQLite 回放已有自动任务，避免错过线程启动前的历史信号。
        QTimer.singleShot(0, self.sync_auto_upload_data_list)
        self.refresh_upload_history()

    def _sync_auto_upload_runtime(self):
        """把最新的连接与目录配置安全传给后台监听线程。"""
        if self.auto_upload_thread is not None:
            self.auto_upload_thread.update_settings(**self._auto_upload_settings_from_config())

    def choose_auto_excel_directory(self):
        directory = QFileDialog.getExistingDirectory(
            self, "选择电量 Excel 监听文件夹", self.auto_excel_directory_input.text().strip()
        )
        if directory:
            self.auto_excel_directory_input.setText(directory)

    def choose_auto_image_directory(self):
        directory = QFileDialog.getExistingDirectory(
            self, "选择几何量图片监听文件夹", self.auto_image_directory_input.text().strip()
        )
        if directory:
            self.auto_image_directory_input.setText(directory)

    def save_auto_upload_settings(self):
        """保存目录配置并立即更新后台监听器。"""
        excel_directory = self.auto_excel_directory_input.text().strip()
        image_directory = self.auto_image_directory_input.text().strip()
        enabled = self.auto_upload_enabled_checkbox.isChecked()
        for label, directory in (("电量 Excel", excel_directory), ("几何量图片", image_directory)):
            if directory and not Path(directory).is_dir():
                QMessageBox.warning(self, "目录不可用", f"{label}监听文件夹不存在或不可访问：\n{directory}")
                return
        if enabled and not (excel_directory or image_directory):
            QMessageBox.warning(self, "缺少目录", "请至少配置一个监听文件夹后再启用自动监听。")
            return
        lower_config.set("auto_upload", "enabled", str(enabled).lower())
        lower_config.set("auto_upload", "excel_directory", excel_directory)
        lower_config.set("auto_upload", "image_directory", image_directory)
        self._sync_auto_upload_runtime()
        self.log("自动文件夹上传设置已保存")
        self.statusBar().showMessage("自动监听设置已保存", 4000)

    @pyqtSlot(str)
    def on_auto_upload_status_changed(self, status: str):
        self.auto_upload_status_label.setText(status)
        self.auto_upload_status_label.setStyleSheet(
            "color: #267a3b;" if "自动监听" in status and "异常" not in status and "不可用" not in status else "color: #b15b00;"
        )

    def _auto_upload_status_display(self, status: str) -> tuple[str, str, bool]:
        """把本地自动任务状态映射为数据列表可读文案和颜色。"""
        status_map = {
            "等待上传": ("已加入自动上传队列", "#1a73e8", False),
            "上传中": ("自动上传中...", "#1a73e8", False),
            "上传成功": ("✓ 自动上传成功", "#188038", True),
            "等待重试": ("等待自动重试", "#c5221f", False),
            "文件不存在": ("源文件不存在", "#c5221f", False),
        }
        return status_map.get(status, (status or "自动任务状态未知", "#555555", False))

    def sync_auto_upload_data_list(self):
        """从 SQLite 回放自动上传记录，保证已有任务也能显示在数据上传列表。"""
        try:
            records = self.upload_history_store.list_auto_recent()
            for record in records:
                path = Path(record.get("file_path") or "")
                if not path:
                    continue
                item_key = str(path.resolve())
                try:
                    data_type = DataType(record.get("data_type"))
                except (TypeError, ValueError):
                    continue
                display_status, color, uploaded = self._auto_upload_status_display(record.get("status", ""))
                item_info = self.auto_data_items.get(item_key)
                if item_info is None:
                    # 历史文件即使被移动或删除，也使用数据库内的大小与名称展示其自动任务状态。
                    meter_data = MeterData(
                        data_type=data_type,
                        file_path=path,
                        timestamp=datetime.now(),
                        description=f"自动上传 - {record.get('file_name') or path.stem}",
                        file_size=int(record.get("file_size") or 0),
                    )
                    list_item = QListWidgetItem(self.data_list_widget)
                    data_widget = MeterDataListItem(meter_data, upload_source="自动上传")
                    list_item.setSizeHint(data_widget.sizeHint())
                    self.data_list_widget.addItem(list_item)
                    self.data_list_widget.setItemWidget(list_item, data_widget)
                    item_info = {"item": list_item, "widget": data_widget, "data": meter_data}
                    self.auto_data_items[item_key] = item_info
                data_widget = item_info["widget"]
                data_widget.set_status(display_status, color)
                data_widget.set_progress(100 if uploaded else 0)
                if uploaded:
                    data_widget.set_uploaded(True)
        except Exception as exc:
            logger.exception(f"同步自动上传数据列表失败: {exc}")
            self.log(f"自动上传列表同步失败: {exc}", error=True)

    @pyqtSlot(str, str)
    def on_auto_file_discovered(self, file_path: str, data_type: str):
        """监听线程发现文件后，从已提交的 SQLite 记录同步对应自动任务行。"""
        self.sync_auto_upload_data_list()
        type_text = "电量 Excel" if data_type == DataType.EXCEL.value else "几何量图片"
        self.log(f"[自动上传] 已加入待上传队列: {type_text} - {Path(file_path).name}")

    @pyqtSlot(str)
    def on_auto_upload_started(self, file_path: str):
        """自动上传开始时同步数据库状态，避免状态停留在排队中。"""
        self.sync_auto_upload_data_list()

    @pyqtSlot(dict)
    def on_auto_upload_result(self, result: dict):
        """自动上传结束后回放最新 SQLite 状态，覆盖信号丢失或重启场景。"""
        self.sync_auto_upload_data_list()
        file_name = result.get("file_name", "未知文件")
        if result.get("success"):
            self.log(f"[自动上传] ✓ {file_name}: {result.get('message', '上传成功')}")
        else:
            self.log(f"[自动上传] ✗ {file_name}: {result.get('message', '上传失败')}，已等待重试", error=True)

    @pyqtSlot(int)
    def on_main_tab_changed(self, tab_index: int):
        """切换上传页时先显示遮罩，再异步加载目标页内容。"""
        self.tab_switch_loading_token += 1
        token = self.tab_switch_loading_token
        page_text = "上传记录" if tab_index == 1 else "数据上传"
        self.show_loading(f"正在切换到{page_text}...")
        QTimer.singleShot(0, lambda: self._finish_main_tab_switch(token, tab_index))

    def _finish_main_tab_switch(self, token: int, tab_index: int):
        """只完成最后一次标签页切换，避免快速点击留下过期遮罩。"""
        if token != self.tab_switch_loading_token:
            return
        try:
            if tab_index == 1:
                self.refresh_upload_history()
        finally:
            self.hide_loading()

    def eventFilter(self, watched, event):
        """记录表滚动或键盘浏览期间不重绘，保持滚动流畅。"""
        if hasattr(self, "upload_history_table") and watched in (
            self.upload_history_table,
            self.upload_history_table.viewport(),
        ):
            if event.type() in (
                QEvent.Type.Wheel,
                QEvent.Type.MouseButtonPress,
                QEvent.Type.KeyPress,
            ):
                self.pause_upload_history_refresh()
        return super().eventFilter(watched, event)

    def pause_upload_history_refresh(self):
        """延后记录表刷新，给用户的滚动与选中操作让出主线程。"""
        if self.main_tabs.currentIndex() != 1:
            return
        self.upload_history_interacting = True
        self.upload_history_interaction_timer.start(800)

    def defer_upload_history_refresh(self):
        """拖动滚动条结束后短暂等待，随后再合并刷新一次。"""
        if self.main_tabs.currentIndex() == 1:
            self.upload_history_interaction_timer.start(250)

    def _resume_upload_history_refresh(self):
        """用户停止操作后恢复刷新；积压变化只刷新一次。"""
        self.upload_history_interacting = False
        if self.upload_history_dirty:
            self.schedule_upload_history_refresh()

    def schedule_upload_history_refresh(self):
        """合并后台上传的连续状态变化，避免自动上传时频繁重绘历史表。"""
        self.upload_history_dirty = True
        if (
            self.main_tabs.currentIndex() != 1
            or self.upload_history_interacting
            or self.upload_history_refresh_scheduled
        ):
            return
        self.upload_history_refresh_scheduled = True
        QTimer.singleShot(500, self.refresh_upload_history)

    def refresh_upload_history(self):
        """仅在记录页可见且用户未滚动时批量刷新本地 SQLite 历史。"""
        self.upload_history_refresh_scheduled = False
        if not hasattr(self, "upload_history_table"):
            return
        if self.main_tabs.currentIndex() != 1 or self.upload_history_interacting:
            self.upload_history_dirty = True
            return
        try:
            records = self.upload_history_store.list_recent()
            self.upload_history_table.setUpdatesEnabled(False)
            self.upload_history_table.blockSignals(True)
            self.upload_history_table.setRowCount(len(records))
            self.upload_history_row_ids = []
            for row_index, record in enumerate(records):
                self.upload_history_row_ids.append(record["id"])
                data_type_text = "电量数据" if record["data_type"] == DataType.EXCEL.value else "几何量数据"
                values = [
                    record.get("updated_at") or record.get("created_at") or "",
                    record.get("source", ""),
                    data_type_text,
                    record.get("file_name", ""),
                    record.get("status", ""),
                    str(record.get("retry_count", 0)),
                    record.get("server_file_id") or "-",
                    record.get("file_path", ""),
                    record.get("message") or "",
                ]
                for column, value in enumerate(values):
                    self.upload_history_table.setItem(row_index, column, QTableWidgetItem(str(value)))
                status_item = self.upload_history_table.item(row_index, 4)
                if record.get("status") == "上传成功":
                    status_item.setForeground(QColor("#188038"))
                elif record.get("status") in ("等待重试", "上传失败", "文件不存在"):
                    status_item.setForeground(QColor("#c5221f"))
            self.upload_history_summary_label.setText(f"本地上传记录：{len(records)} 条，按最新时间排序")
            self.upload_history_dirty = False
        except Exception as exc:
            logger.exception(f"刷新上传历史失败: {exc}")
            self.upload_history_summary_label.setText(f"读取上传记录失败：{exc}")
        finally:
            self.upload_history_table.blockSignals(False)
            self.upload_history_table.setUpdatesEnabled(True)
    def delete_selected_upload_history_records(self):
        """多选删除仅影响本地 SQLite 记录，不影响源文件和服务器数据。"""
        selected_rows = sorted({index.row() for index in self.upload_history_table.selectedIndexes()})
        record_ids = [self.upload_history_row_ids[row] for row in selected_rows if row < len(self.upload_history_row_ids)]
        if not record_ids:
            QMessageBox.information(self, "提示", "请先选择要删除的上传记录。")
            return
        reply = QMessageBox.question(
            self,
            "确认删除",
            f"确定删除选中的 {len(record_ids)} 条本地上传记录吗？\n不会删除源文件或服务器数据。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        deleted_count = self.upload_history_store.delete_records(record_ids)
        self.log(f"已删除 {deleted_count} 条本地上传记录")
        self.refresh_upload_history()

    def show_upload_history_detail(self, row: int, _column: int):
        values = []
        headers = [self.upload_history_table.horizontalHeaderItem(index).text() for index in range(self.upload_history_table.columnCount())]
        for column, header in enumerate(headers):
            item = self.upload_history_table.item(row, column)
            values.append(f"{header}: {item.text() if item else ''}")
        QMessageBox.information(self, "上传记录详情", "\n".join(values))
    def show_loading(self, text: str = "处理中..."):
        """显示统一 loading 遮罩，避免按钮点击后没有反馈。"""
        if self.loading_overlay is None:
            self.loading_overlay = LoadingOverlay(self, text)
        else:
            self.loading_overlay.update_text(text)
        self.loading_overlay.resize_to_parent()
        self.loading_overlay.raise_()
        self.loading_overlay.show()
        QApplication.processEvents()

    def update_loading_text(self, text: str):
        """更新 loading 文案。"""
        if self.loading_overlay is not None:
            self.loading_overlay.update_text(text)
            QApplication.processEvents()

    def hide_loading(self):
        """隐藏 loading 遮罩。"""
        if self.loading_overlay is not None:
            self.loading_overlay.hide()

    def _set_buttons_enabled(self, buttons, enabled: bool):
        if buttons is None:
            return
        if not isinstance(buttons, (list, tuple, set)):
            buttons = [buttons]
        for button in buttons:
            if button is not None:
                button.setEnabled(enabled)

    def run_background_task(self, target, on_success, on_error=None, loading_text="处理中...", show_loading=True, buttons=None):
        """统一封装后台任务线程，让网络请求通过信号槽回到主线程。"""
        if show_loading:
            self.show_loading(loading_text)
        self._set_buttons_enabled(buttons, False)

        thread = AsyncTaskThread(target, parent=self)
        self.active_async_threads.append(thread)

        def cleanup():
            self._set_buttons_enabled(buttons, True)
            if show_loading:
                self.hide_loading()
            if thread in self.active_async_threads:
                self.active_async_threads.remove(thread)
            thread.deleteLater()

        def handle_success(result):
            cleanup()
            on_success(result)

        def handle_error(message):
            cleanup()
            if on_error is not None:
                on_error(message)
            else:
                QMessageBox.critical(self, "错误", message)

        thread.result_ready.connect(handle_success)
        thread.error_occurred.connect(handle_error)
        thread.start()
        return thread

    def load_config(self):
        """加载配置"""
        self.server_input.setText(lower_config.server_url)
        self.device_id_input.setText(lower_config.device_id)
        self.device_name_input.setText(lower_config.device_name)
        self.location_input.setText(lower_config.location)
        self.auto_excel_directory_input.setText(lower_config.get("auto_upload", "excel_directory", ""))
        self.auto_image_directory_input.setText(lower_config.get("auto_upload", "image_directory", ""))
        self.auto_upload_enabled_checkbox.setChecked(
            lower_config.get("auto_upload", "enabled", "false").lower() == "true"
        )
        self.registration_status_label.setText("未查询")
        self.registration_detail_label.setText("请先提交注册申请")

        if lower_config.hardware_key:
            self.hardware_key_label.setText(lower_config.hardware_key[:16] + "...")

    @staticmethod
    def _normalize_city_name(city: str) -> str:
        """把网络接口返回的城市名整理成中文市/区县展示文本。"""
        city = (city or "").strip()
        if not city:
            return ""

        english_city_map = {
            # 常见直辖市/城市英文名映射。网络定位服务偶尔即使请求中文也会返回拼音。
            "beijing": "北京市",
            "shanghai": "上海市",
            "tianjin": "天津市",
            "chongqing": "重庆市",
            "changsha": "长沙市",
            "suzhou": "苏州市",
            "shenzhen": "深圳市",
            "guangzhou": "广州市",
            "hangzhou": "杭州市",
            "nanjing": "南京市",
            "wuhan": "武汉市",
            "chengdu": "成都市",
            "xian": "西安市",
            "xi'an": "西安市",
            # 上海长宁经常被 IP 定位接口返回为 Changning。
            "changning": "长宁区",
        }
        normalized_key = city.lower().replace(" city", "").strip()
        if normalized_key in english_city_map:
            return english_city_map[normalized_key]

        has_chinese = any('\u4e00' <= char <= '\u9fff' for char in city)
        if not has_chinese:
            # 未知英文名不要写入配置，继续尝试其他中文定位接口或走失败回退。
            return ""

        municipality_map = {"北京": "北京市", "上海": "上海市", "天津": "天津市", "重庆": "重庆市"}
        if city in municipality_map:
            return municipality_map[city]
        if has_chinese and not city.endswith(("市", "区", "县", "州", "盟")):
            return f"{city}市"
        return city

    def detect_city_by_network(self) -> str:
        """根据公网 IP 自动定位城市，失败时抛出异常让调用方保留配置值。"""
        endpoints = [
            # 优先使用中文定位接口，避免英文拼音城市名写入配置。
            ("百度定位", "https://qifu-api.baidubce.com/ip/local/geo/v1/district", lambda data: (data.get("data") or {}).get("city")),
            ("ip-api", "http://ip-api.com/json/?lang=zh-CN", lambda data: data.get("city")),
            ("ipapi", "https://ipapi.co/json/", lambda data: data.get("city")),
        ]
        headers = {"User-Agent": "NQI-Lower-Client/1.0"}
        last_error = ""
        for name, url, parser in endpoints:
            try:
                response = requests.get(url, headers=headers, timeout=4)
                response.raise_for_status()
                city = self._normalize_city_name(parser(response.json()))
                if city:
                    return city
            except Exception as exc:
                last_error = f"{name}: {exc}"
                logger.warning(f"网络定位接口失败: {last_error}")
        raise Exception(last_error or "未获取到城市信息")

    def auto_detect_location(self):
        """启动后自动定位城市；不阻塞界面，失败则沿用配置中的城市。"""
        original_location = self.location_input.text().strip()
        self.statusBar().showMessage("正在自动定位所在城市...")

        def task():
            return self.detect_city_by_network()

        def on_success(city):
            current_location = self.location_input.text().strip()
            # 如果用户在定位返回前手动修改过城市，尊重用户输入，不强行覆盖。
            if not current_location or current_location == original_location:
                self.location_input.setText(city)
                lower_config.location = city
            else:
                lower_config.location = current_location
            self.log(f"自动定位所在城市: {lower_config.location}")
            if not self.authenticated:
                self.statusBar().showMessage(f"所在城市: {lower_config.location}")

        def on_error(message):
            fallback = original_location or lower_config.location
            self.location_input.setText(fallback)
            self.log(f"自动定位城市失败，沿用配置城市: {fallback}；原因: {message}", error=True)
            if not self.authenticated:
                self.statusBar().showMessage(f"所在城市: {fallback}")

        self.run_background_task(
            task,
            on_success=on_success,
            on_error=on_error,
            loading_text="正在自动定位所在城市...",
            show_loading=False,
        )

    def _start_registration_status_polling(self):
        """待审批期间定时刷新注册状态。"""
        if not self.registration_status_timer.isActive():
            self.registration_status_timer.start()

    def _stop_registration_status_polling(self):
        """审批完成后停止自动刷新。"""
        if self.registration_status_timer.isActive():
            self.registration_status_timer.stop()

    def _apply_registration_status(self, result: dict):
        """把服务器返回的注册状态映射到界面和按钮状态。"""
        status = result.get("registration_status", "unknown")
        status_text = result.get("status_text", "未返回状态说明")
        queue_position = result.get("queue_position")
        review_message = result.get("review_message") or ""
        requested_at = result.get("requested_at") or "-"
        reviewed_at = result.get("reviewed_at") or "-"

        color_map = {
            "approved": "green",
            "pending": "#b45309",
            "rejected": "red",
            "conflict": "red",  # 设备ID已被其他硬件占用。
            "unregistered": "gray",
        }
        label_map = {
            "approved": "已通过",
            "pending": "待审批",
            "rejected": "已驳回",
            "conflict": "设备ID已占用",
            "unregistered": "未注册",
        }
        self.registration_status_label.setText(label_map.get(status, status))
        self.registration_status_label.setStyleSheet(
            f"color: {color_map.get(status, '#1f2937')}; font-weight: bold;"
        )

        detail_lines = [status_text, f"申请时间: {requested_at}"]
        if queue_position not in (None, 0):
            detail_lines.append(f"当前排队序号: {queue_position}")
        if reviewed_at != "-":
            detail_lines.append(f"审批时间: {reviewed_at}")
        if review_message:
            detail_lines.append(f"审批意见: {review_message}")
        self.registration_detail_label.setText(" | ".join(detail_lines))

        if status == "pending":
            self._start_registration_status_polling()
            self.authenticated = False
            self.add_excel_btn.setEnabled(False)
            self.add_image_btn.setEnabled(False)
            self.upload_btn.setEnabled(False)
            self.statusBar().showMessage("注册待审批")
        elif status == "approved":
            self._stop_registration_status_polling()
            self.statusBar().showMessage("设备审批已通过，可连接服务器")
        else:
            self.authenticated = False
            self.add_excel_btn.setEnabled(False)
            self.add_image_btn.setEnabled(False)
            self.upload_btn.setEnabled(False)
            self.statusBar().showMessage(status_text)
            if status != "unregistered":
                self._stop_registration_status_polling()

    def _query_registration_status_sync(self, server_url: str, device_id: str, hardware_key: str):
        """同步查询注册状态，供后台线程复用。"""
        client = APIClient(server_url)
        return client.get_registration_status(device_id, hardware_key)

    def query_registration_status(self, silent: bool = False, show_dialog: bool = True):
        """查询当前设备的注册审批状态或进度。"""
        server_url = self.server_input.text().strip()
        device_id = self.device_id_input.text().strip()
        hardware_key = lower_config.hardware_key

        if not all([server_url, device_id, hardware_key]):
            if not silent:
                QMessageBox.warning(self, "提示", "请先填写服务器地址、设备ID并生成硬件密钥")
            return None

        def task():
            return self._query_registration_status_sync(server_url, device_id, hardware_key)

        def on_success(result):
            self.client = APIClient(server_url)
            status = result.get("registration_status", "unknown")
            request_id = result.get("request_id")
            status_changed = (
                status != self.last_registration_status
                or request_id != self.last_registration_request_id
            )
            self.last_registration_status = status
            self.last_registration_request_id = request_id
            self._apply_registration_status(result)

            if status_changed or not silent:
                self.log(f"注册状态更新: {result.get('status_text', status)}")

            if show_dialog and status_changed:
                if status == "approved":
                    QMessageBox.information(self, "审批通过", "设备注册审批已通过，现在可以连接服务器。")
                elif status == "rejected":
                    QMessageBox.warning(self, "审批驳回", result.get("review_message") or "注册申请已被驳回")

        def on_error(message):
            if not silent:
                self.log(f"查询注册状态失败: {message}", error=True)
                QMessageBox.critical(self, "错误", f"查询注册状态失败: {message}")

        return self.run_background_task(
            task,
            on_success=on_success,
            on_error=on_error,
            loading_text="正在查询注册状态...",
            show_loading=not silent,
            buttons=[self.query_status_btn, self.register_btn, self.connect_btn],
        )

    def save_current_config(self):
        """保存当前配置。"""
        self.show_loading("正在保存配置...")
        try:
            lower_config.server_url = self.server_input.text().strip()
            lower_config.device_id = self.device_id_input.text().strip()
            lower_config.device_name = self.device_name_input.text().strip()
            lower_config.location = self.location_input.text().strip()
            self._sync_auto_upload_runtime()
            self.log("配置已保存到 lower_config.ini")
            QMessageBox.information(self, "成功", "配置已保存！")
        except Exception as e:
            self.log(f"保存配置失败: {e}", error=True)
            QMessageBox.critical(self, "错误", f"保存失败: {e}")
        finally:
            self.hide_loading()

    def generate_hardware_key(self):
        """生成硬件密钥。"""
        self.show_loading("正在生成硬件密钥...")
        try:
            hardware_key = hardware_key_generator.get_machine_id()
            if hardware_key:
                lower_config.hardware_key = hardware_key
                self.hardware_key_label.setText(hardware_key[:16] + "...")
                self.log("硬件密钥生成成功")
                QGuiApplication.clipboard().setText(hardware_key)
                QMessageBox.information(self, "成功", f"硬件密钥已生成\n{hardware_key},且已经复制到剪贴板")
            else:
                raise Exception("无法生成硬件密钥")
        except Exception as e:
            self.log(f"生成硬件密钥失败: {e}", error=True)
            QMessageBox.critical(self, "错误", f"生成失败: {e}")
        finally:
            self.hide_loading()

    def register_device(self):
        """异步提交设备注册申请。"""
        server_url = self.server_input.text().strip()
        device_id = self.device_id_input.text().strip()
        device_name = self.device_name_input.text().strip()
        location = self.location_input.text().strip()
        hardware_key = lower_config.hardware_key

        if not all([server_url, device_id, device_name, hardware_key, location]):
            QMessageBox.warning(self, "警告", "请填写服务器、设备信息、所在城市并生成硬件密钥")
            return

        lower_config.device_id = device_id
        lower_config.device_name = device_name
        lower_config.location = location

        def task():
            client = APIClient(server_url)
            device_ip = self.get_local_ip()
            # 注册申请带上城市，服务器会保存到待审批记录。
            result = client.register_device(device_id, device_name, hardware_key, device_ip, location)
            return {"result": result, "server_url": server_url}

        def on_success(payload):
            self.client = APIClient(payload["server_url"])
            result = payload["result"]
            registration_state = result.get("status")

            if registration_state == "pending":
                self.log(f"设备注册申请已提交，等待上位机审批: {result.get('request_id')}")
                self.query_registration_status(silent=True, show_dialog=False)
                QMessageBox.information(self, "等待审批", "注册申请已提交，请等待上位机审批。")
            elif registration_state == "approved":
                self.log(f"设备已审批，可直接连接服务器: {result.get('device_id')}")
                self.query_registration_status(silent=True, show_dialog=False)
                QMessageBox.information(self, "已审批", "该设备已经审批通过，可以直接连接服务器。")
            else:
                detail = result.get('detail', result.get('message', '注册失败'))
                self.log(f"设备注册失败: {detail}", error=True)
                QMessageBox.critical(self, "错误", f"注册失败: {detail}")

        def on_error(message):
            self.log(f"设备注册失败: {message}", error=True)
            QMessageBox.critical(self, "错误", f"注册失败: {message}")

        self.run_background_task(
            task,
            on_success=on_success,
            on_error=on_error,
            loading_text="正在提交设备注册申请...",
            buttons=[self.register_btn, self.query_status_btn, self.connect_btn],
        )

    def connect_to_server(self):
        """异步连接到服务器。"""
        server_url = self.server_input.text().strip()
        device_id = self.device_id_input.text().strip()
        location = self.location_input.text().strip()
        hardware_key = lower_config.hardware_key

        if not all([server_url, device_id, hardware_key, location]):
            QMessageBox.warning(self, "警告", "请填写服务器、设备ID、所在城市并生成硬件密钥")
            return

        lower_config.server_url = server_url
        lower_config.device_id = device_id
        lower_config.location = location

        def task():
            registration_result = self._query_registration_status_sync(server_url, device_id, hardware_key)
            if registration_result.get("registration_status") != "approved":
                raise Exception(registration_result.get("status_text", "设备尚未审批，暂时不能连接服务器"))

            client = APIClient(server_url)
            device_ip = self.get_local_ip()
            # 认证连接时刷新服务器设备表里的城市位置。
            auth_result = client.authenticate_device(device_id, hardware_key, device_ip, location)
            return {
                "server_url": server_url,
                "registration_result": registration_result,
                "auth_result": auth_result,
            }

        def on_success(payload):
            self.client = APIClient(payload["server_url"])
            self.authenticated = True
            self._sync_auto_upload_runtime()
            self._apply_registration_status(payload["registration_result"])
            self.log(f"连接成功: {payload['auth_result'].get('message')}")
            self._stop_registration_status_polling()

            if self.long_polling_thread:
                self.long_polling_thread.stop()
                self.long_polling_thread.join(timeout=3)

            self.long_polling_thread = LowerLongPollingThread(
                server_url=payload["server_url"],
                device_id=device_id,
                hardware_key=hardware_key,
                poll_interval=3,
                location=location
            )
            self.long_polling_thread.set_connected_callback(lambda: self.connected_signal.emit())
            self.long_polling_thread.set_disconnected_callback(lambda: self.disconnected_signal.emit())
            self.long_polling_thread.set_error_callback(lambda message: self.error_signal.emit(str(message)))
            self.long_polling_thread.set_registration_pending_callback(lambda: self.registration_pending_signal.emit())
            self.long_polling_thread.start()
            self.log("正在建立HTTP长轮询连接...")
            self.statusBar().showMessage("已连接 ✓ (HTTP长轮询)")

            self.add_excel_btn.setEnabled(True)
            self.add_image_btn.setEnabled(True)
            self.remove_file_btn.setEnabled(True)
            self.clear_files_btn.setEnabled(True)
            self.update_data_count()
            QMessageBox.information(self, "成功", "连接服务器成功！")

        def on_error(message):
            self.authenticated = False
            self._sync_auto_upload_runtime()
            self.log(f"连接失败: {message}", error=True)
            self.statusBar().showMessage("连接失败 ✗")
            self.query_registration_status(silent=True, show_dialog=False)
            QMessageBox.critical(self, "错误", f"连接失败: {message}")

        self.run_background_task(
            task,
            on_success=on_success,
            on_error=on_error,
            loading_text="正在连接服务器并校验审批状态...",
            buttons=[self.connect_btn, self.register_btn, self.query_status_btn],
        )

    def add_excel_files(self):
        """添加电量数据（Excel）。"""
        start_dir = lower_config.last_excel_dir or ""
        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择电量数据文件",
            start_dir,
            "Excel文件 (*.xlsx *.xls)"
        )

        if file_paths:
            self.show_loading("正在加载电量数据文件...")
            try:
                lower_config.last_excel_dir = str(Path(file_paths[0]).parent)
                for file_path in file_paths:
                    self.add_meter_data(Path(file_path), DataType.EXCEL)
            finally:
                self.hide_loading()

    def add_image_files(self):
        """添加几何量数据（图片）。"""
        start_dir = lower_config.last_image_dir or ""
        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择几何量数据文件",
            start_dir,
            "图片文件 (*.jpg *.jpeg *.png *.bmp)"
        )

        if file_paths:
            self.show_loading("正在加载几何量数据文件...")
            try:
                lower_config.last_image_dir = str(Path(file_paths[0]).parent)
                for file_path in file_paths:
                    self.add_meter_data(Path(file_path), DataType.IMAGE)
            finally:
                self.hide_loading()

    def add_meter_data(self, file_path: Path, data_type: DataType):
        """添加三相表数据"""
        # 检查是否已存在
        if str(file_path) in self.data_items:
            self.log(f"数据已在列表中: {file_path.name}")
            return

        # 创建数据对象
        if data_type == DataType.EXCEL:
            description = lower_config.get_excel_description()
        else:
            description = lower_config.get_image_description()

        meter_data = self.meter_data_manager.create_meter_data(file_path, description)

        if meter_data is None:
            self.log(f"创建数据失败: {file_path.name}", error=True)
            return

        # 创建列表项
        item = QListWidgetItem(self.data_list_widget)
        data_widget = MeterDataListItem(meter_data, upload_source="手动上传")

        item.setSizeHint(data_widget.sizeHint())
        self.data_list_widget.addItem(item)
        self.data_list_widget.setItemWidget(item, data_widget)

        self.data_items[str(file_path)] = {
            'item': item,
            'widget': data_widget,
            'data': meter_data
        }

        self.update_data_count()
        data_type_name = "电量数据" if data_type == DataType.EXCEL else "几何量数据"
        self.log(f"添加{data_type_name}: {file_path.name} ({meter_data.file_size / 1024:.1f} KB)")

    def remove_selected_data(self):
        """移除勾选框选中的数据，忽略鼠标单击造成的列表行选中。"""
        checked_rows = []
        for row in range(self.data_list_widget.count()):
            item = self.data_list_widget.item(row)
            widget = self.data_list_widget.itemWidget(item)
            if widget and widget.checkbox.isChecked():
                checked_rows.append(row)

        if not checked_rows:
            QMessageBox.warning(self, "警告", "请勾选要移除的数据")
            return

        self.show_loading("正在移除勾选数据...")
        try:
            # 从后往前删除，避免前面行删除后导致后续行号偏移。
            for row in reversed(checked_rows):
                item = self.data_list_widget.item(row)
                widget = self.data_list_widget.itemWidget(item)

                if widget:
                    file_path = str(widget.meter_data.file_path)
                    if file_path in self.data_items:
                        del self.data_items[file_path]

                self.data_list_widget.takeItem(row)

            self.update_data_count()
            self.log(f"移除了 {len(checked_rows)} 个勾选数据")
        finally:
            self.hide_loading()

    def clear_data_list(self):
        """清空数据列表"""
        if self.data_items or self.auto_data_items:
            reply = QMessageBox.question(
                self,
                "确认",
                f"确定要清空所有 {len(self.data_items) + len(self.auto_data_items)} 个数据吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )

            if reply == QMessageBox.StandardButton.Yes:
                self.show_loading("正在清空数据列表...")
                try:
                    self.data_list_widget.clear()
                    self.data_items.clear()
                    self.auto_data_items.clear()
                    self.update_data_count()
                    self.log("已清空数据列表")
                finally:
                    self.hide_loading()

    def select_all_data(self):
        """全选所有尚未上传成功的数据。"""
        self.show_loading("正在全选数据...")
        try:
            selected_count = 0
            skipped_count = 0
            for file_info in self.data_items.values():
                widget = file_info['widget']
                if widget.uploaded:
                    # 已上传项保持未选中和禁用，防止全选绕过重复上传限制。
                    widget.checkbox.setChecked(False)
                    skipped_count += 1
                    continue
                widget.checkbox.setChecked(True)
                selected_count += 1

            if skipped_count:
                tip = f"已选择 {selected_count} 个待上传文件，跳过 {skipped_count} 个上传成功的文件"
                self.statusBar().showMessage(tip, 5000)
                self.log(f"{tip}；上传成功的文件不能重复选择上传")
            else:
                self.log(f"已全选 {selected_count} 个数据")
        finally:
            self.hide_loading()
    
    def deselect_all_data(self):
        """全不选数据"""
        self.show_loading("正在取消选择...")
        try:
            for file_info in self.data_items.values():
                widget = file_info['widget']
                # 已上传项也要响应全不选，否则上传完成后用户会看到按钮点击没有反馈。
                widget.checkbox.setChecked(False)
            self.log("已全不选数据")
        finally:
            self.hide_loading()
    
    def update_data_count(self):
        """更新数据计数"""
        excel_count = sum(1 for item in self.data_items.values() if item['data'].is_excel)
        image_count = sum(1 for item in self.data_items.values() if item['data'].is_image)

        self.excel_count_label.setText(f"电量数据: {excel_count}")
        self.image_count_label.setText(f"几何量数据: {image_count}")

        total = excel_count + image_count
        self.upload_btn.setEnabled(total > 0 and self.authenticated)
        self.select_all_btn.setEnabled(total > 0)
        self.deselect_all_btn.setEnabled(total > 0)

    def upload_data(self):
        """上传选中的数据。"""
        if not self.authenticated:
            QMessageBox.warning(self, "警告", "请先连接服务器")
            return

        if not self.data_items:
            QMessageBox.warning(self, "警告", "请先添加要上传的数据")
            return

        selected_items = [
            file_info for file_info in self.data_items.values()
            if file_info['widget'].is_selected()
        ]

        if not selected_items:
            uploaded_count = sum(
                1 for file_info in self.data_items.values()
                if file_info['widget'].uploaded
            )
            if uploaded_count == len(self.data_items):
                QMessageBox.information(
                    self,
                    "提示",
                    "列表中的文件均已上传成功，不能重复选择上传。"
                )
            else:
                QMessageBox.warning(self, "警告", "请选择尚未上传的文件")
            return

        upload_location = self.location_input.text().strip() or lower_config.location
        if not upload_location:
            QMessageBox.warning(self, "警告", "请先填写所在城市")
            return
        lower_config.location = upload_location

        self.show_loading("正在准备上传任务...")
        try:
            self.upload_btn.setEnabled(False)
            self.stop_upload_btn.setEnabled(True)
            self.add_excel_btn.setEnabled(False)
            self.add_image_btn.setEnabled(False)

            self.log(f"开始上传 {len(selected_items)} 个数据...")
            excel_count = sum(1 for item in selected_items if item['data'].is_excel)
            image_count = len(selected_items) - excel_count
            self.log(f"  - 电量数据: {excel_count} 个")
            self.log(f"  - 几何量数据: {image_count} 个")

            self.pending_upload_count = len(selected_items)
            for file_info in selected_items:
                data_widget = file_info['widget']
                meter_data = file_info['data']
                data_widget.set_status("上传中...", "blue")
                data_widget.set_progress(0)

                worker = UploadWorker(
                    self.client,
                    lower_config.device_id,
                    lower_config.hardware_key,
                    meter_data,
                    location=upload_location
                )
                worker.signals.progress.connect(self.on_upload_progress)
                worker.signals.finished.connect(self.on_upload_finished)
                self.thread_pool.start(worker)
        finally:
            self.hide_loading()

    def stop_upload(self):
        """停止上传。"""
        reply = QMessageBox.question(
            self,
            "确认",
            "确定要停止所有上传任务吗？\n（正在上传的数据会继续完成）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            self.show_loading("正在停止未开始的上传任务...")
            try:
                self.thread_pool.clear()
                self.log("已停止上传任务")
                self.restore_upload_buttons()
            finally:
                self.hide_loading()

    @pyqtSlot(str, int)
    def on_upload_progress(self, file_name: str, progress: int):
        """上传进度更新"""
        for file_info in self.data_items.values():
            if file_info['data'].file_path.name == file_name:
                file_info['widget'].set_progress(progress)
                break

    @pyqtSlot(str, bool, str)
    def on_upload_finished(self, file_name: str, success: bool, message: str):
        """上传完成"""
        for file_path, file_info in self.data_items.items():
            if file_info['data'].file_path.name == file_name:
                if success:
                    file_info['widget'].set_status("✓ 成功", "green")
                    file_info['widget'].set_progress(100)
                    file_info['widget'].set_uploaded(True)  # 标记为已上传，取消勾选

                    self.log(f"✓ {file_name}: {message}")
                else:
                    # 上传失败的文件仍可勾选后重新上传。
                    file_info['widget'].set_uploaded(False)
                    file_info['widget'].set_status("✗ 失败", "red")
                    file_info['widget'].set_progress(0)
                    self.log(f"✗ {file_name}: {message}", error=True)
                self.upload_history_store.record_manual_result(
                    file_info['data'].file_path,
                    file_info['data'].data_type,
                    success,
                    message,
                )
                self.refresh_upload_history()
                break

        # 检查是否全部完成
        self.check_all_uploads_finished()

    def check_all_uploads_finished(self):
        """检查是否所有上传都已完成"""
        all_finished = True
        success_count = 0
        fail_count = 0

        for file_info in self.data_items.values():
            status = file_info['widget'].status_label.text()
            if "上传中" in status:
                all_finished = False
            elif "成功" in status:
                success_count += 1
            elif "失败" in status:
                fail_count += 1

        if all_finished and (success_count > 0 or fail_count > 0):
            self.restore_upload_buttons()

            result_msg = f"上传完成！\n成功: {success_count} 个\n失败: {fail_count} 个"
            self.log(result_msg)

            QMessageBox.information(self, "上传完成", result_msg)

    def restore_upload_buttons(self):
        """恢复上传按钮状态"""
        self.upload_btn.setEnabled(True)
        self.stop_upload_btn.setEnabled(False)
        self.add_excel_btn.setEnabled(True)
        self.add_image_btn.setEnabled(True)

    # def refresh_cache_info(self):
    #     """刷新缓存信息"""
    #     try:
    #         info = self.meter_data_manager.get_cache_info()
    #
    #         excel_size_mb = info['excel_size'] / (1024 * 1024)
    #         image_size_mb = info['image_size'] / (1024 * 1024)
    #         total_size_mb = info['total_size'] / (1024 * 1024)
    #
    #         info_text = (
    #             f"电量数据: {info['excel_count']} 个 ({excel_size_mb:.2f}MB) | "
    #             f"几何量数据: {info['image_count']} 个 ({image_size_mb:.2f}MB) | "
    #             f"总计: {info['total_count']} 个 ({total_size_mb:.2f}MB)"
    #         )
    #
    #         self.cache_info_label.setText(info_text)
    #     except Exception as e:
    #         self.log(f"刷新缓存信息失败: {e}", error=True)
    #
    # def clear_cache(self):
    #     """清除缓存"""
    #     reply = QMessageBox.question(
    #         self,
    #         "确认",
    #         "确定要清除所有本地缓存吗？\n此操作不可撤销！",
    #         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    #     )
    #
    #     if reply == QMessageBox.StandardButton.Yes:
    #         try:
    #             self.meter_data_manager.clear_cache()
    #             self.refresh_cache_info()
    #             self.log("已清除本地缓存")
    #             QMessageBox.information(self, "成功", "缓存已清除！")
    #         except Exception as e:
    #             self.log(f"清除缓存失败: {e}", error=True)
    #             QMessageBox.critical(self, "错误", f"清除失败: {e}")

    def get_local_ip(self):
        """获取本机IP"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "127.0.0.1"

    def log(self, message: str, error: bool = False):
        """记录日志"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if error:
            log_message = f"[{timestamp}] ❌ {message}"
            logger.error(message)
        else:
            log_message = f"[{timestamp}] ℹ️ {message}"
            logger.info(message)

        self.log_text.append(log_message)

    def closeEvent(self, event):
        """关闭窗口事件"""
        if self.thread_pool.activeThreadCount() > 0:
            reply = QMessageBox.question(
                self,
                "确认退出",
                "还有数据正在上传，确定要退出吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )

            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return

        # 确认退出后立即遮罩界面，防止用户在各后台线程收尾时继续操作。
        self.show_loading("正在安全退出，请稍候...")
        try:
            # 停止自动监听线程，避免程序关闭后继续访问目录或服务器。
            if self.auto_upload_thread:
                self.update_loading_text("正在停止自动文件上传监听...")
                self.auto_upload_thread.stop()
                self.auto_upload_thread.wait(3000)

            # 关闭连接
            if self.long_polling_thread:
                self.update_loading_text("正在关闭服务器连接...")
                self.log("正在停止HTTP心跳...")
                self.long_polling_thread.stop()
                self.long_polling_thread.join(timeout=3)

            self.update_loading_text("正在结束手动上传任务...")
            self.thread_pool.waitForDone(3000)
        finally:
            self.hide_loading()
        event.accept()
    
    def on_connected(self):
        """连接成功回调"""
        self.log("[连接] 长连接已建立")
        self.statusBar().showMessage("已连接 ✓ (HTTP长轮询)")

    def on_disconnected(self):
        """断开连接回调"""
        self.authenticated = False
        self._sync_auto_upload_runtime()
        self.add_excel_btn.setEnabled(False)
        self.add_image_btn.setEnabled(False)
        self.upload_btn.setEnabled(False)
        self.log("[连接] 连接已断开", error=True)
        self.statusBar().showMessage("连接已断开 ✗")

    def on_error(self, error: str):
        """错误回调"""
        self.authenticated = False
        self._sync_auto_upload_runtime()
        self.add_excel_btn.setEnabled(False)
        self.add_image_btn.setEnabled(False)
        self.upload_btn.setEnabled(False)
        self.log(f"[连接] 错误: {error}", error=True)
        self.statusBar().showMessage("连接异常 ✗")

    def on_registration_pending(self):
        """长轮询发现当前设备未审批时，自动刷新注册进度。"""
        self.authenticated = False
        self._sync_auto_upload_runtime()
        self.add_excel_btn.setEnabled(False)
        self.add_image_btn.setEnabled(False)
        self.upload_btn.setEnabled(False)
        self.log("[注册] 当前设备仍处于待审批状态")
        self.query_registration_status(silent=True, show_dialog=False)
        self.statusBar().showMessage("注册待审批")
def quit_qt_application(window:LowerComputerWindow):
    """退出应用程序"""
    if window.authenticated:
        
        # 停止HTTP心跳（心跳停止时会自动发送离线通知）
        if window.long_polling_thread:
            logger.info("停止HTTP心跳...")
            window.long_polling_thread.stop()
            window.long_polling_thread.join(timeout=3)
        
        # 额外调用 API 设置离线（双重保险）
        try:
            if window.client:
                device_id = window.device_id_input.text().strip()
                hardware_key = lower_config.hardware_key
                window.client.set_device_offline(device_id=device_id, hardware_key=hardware_key)
                logger.info("设备已设置为离线")
        except Exception as e:
            logger.error(f"设置设备离线失败: {e}")
def main():
    app = QApplication(sys.argv)

    window = LowerComputerWindow()
    window.show()
    # 绑定退出事件
    app.aboutToQuit.connect(lambda: quit_qt_application(window))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()







