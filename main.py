import sys
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QLabel, QLineEdit,
                             QTextEdit, QFileDialog, QMessageBox, QGroupBox,
                             QProgressBar, QListWidget, QListWidgetItem, QSplitter,
                             QTabWidget, QFrame, QRadioButton, QButtonGroup, QCheckBox)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QThreadPool, QRunnable, pyqtSlot
from PyQt6.QtGui import QFont, QColor, QIcon, QPixmap, QGuiApplication
from pathlib import Path
from loguru import logger
import socket
from datetime import datetime

from config.config import lower_config
from security.hardware_key import hardware_key_generator
from api.api_client import APIClient
from api.websocket_client import DeviceWebSocketThread
from api.long_polling_client import LowerLongPollingThread
from metadata.meter_data import MeterDataManager, DataType, MeterData

# 配置日志
logger.add("logs/lower_{time}.log", rotation="10 MB", retention="30 days", encoding='utf-8')


class UploadWorker(QRunnable):
    """文件上传工作线程"""

    class Signals(QThread):
        progress = pyqtSignal(str, int)  # 文件名, 进度
        finished = pyqtSignal(str, bool, str)  # 文件名, 成功, 消息

    def __init__(self, client: APIClient, device_id: str, hardware_key: str,
                 meter_data: MeterData):
        super().__init__()
        self.signals = UploadWorker.Signals()
        self.client = client
        self.device_id = device_id
        self.hardware_key = hardware_key
        self.meter_data = meter_data

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
                self.meter_data.description
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


class MeterDataListItem(QFrame):
    """三相表数据列表项"""

    def __init__(self, meter_data: MeterData, parent=None):
        super().__init__(parent)
        self.meter_data = meter_data
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

    def set_status(self, status: str, color: str = "black"):
        """设置状态"""
        self.status_label.setText(status)
        self.status_label.setStyleSheet(f"color: {color};")
    
    def set_uploaded(self, uploaded: bool = True):
        """设置为已上传"""
        self.uploaded = uploaded
        if uploaded:
            self.checkbox.setChecked(False)
            # self.checkbox.setEnabled(False)
    
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

    def __init__(self):
        super().__init__()
        self.client = None
        self.ws_thread = None  # WebSocket 长连接线程（向后兼容）
        self.long_polling_thread = None  # HTTP长轮询心跳线程
        self.connection_mode = 'long_polling'  # 连接模式：'websocket' 或 'long_polling'
        self.authenticated = False
        self.data_items = {}  # 文件路径 -> MeterDataListItem
        self.meter_data_manager = MeterDataManager(lower_config.cache_dir)
        self.thread_pool = QThreadPool()
        self.thread_pool.setMaxThreadCount(lower_config.concurrent_uploads)
        self.init_ui()
        self.load_config()

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

        self.connect_btn = QPushButton("连接服务器")
        self.connect_btn.clicked.connect(self.connect_to_server)
        button_layout.addWidget(self.connect_btn)

        self.save_config_btn = QPushButton("保存配置")
        self.save_config_btn.clicked.connect(self.save_current_config)
        button_layout.addWidget(self.save_config_btn)

        button_layout.addStretch()
        config_layout.addLayout(button_layout)

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
        self.upload_btn.setEnabled(False)
        self.upload_btn.setStyleSheet("background-color: #4CAF50; color: white;")
        upload_action_layout.addWidget(self.upload_btn)

        self.stop_upload_btn = QPushButton("停止上传")
        self.stop_upload_btn.setMinimumHeight(40)
        self.stop_upload_btn.clicked.connect(self.stop_upload)
        self.stop_upload_btn.setEnabled(False)
        self.stop_upload_btn.setStyleSheet("background-color: #f44336; color: white;")
        upload_action_layout.addWidget(self.stop_upload_btn)

        upload_layout.addLayout(upload_action_layout)

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

        main_layout.addWidget(splitter)

        # 状态栏
        self.statusBar().showMessage("未连接")

        # # 刷新缓存信息
        # self.refresh_cache_info()

    def load_config(self):
        """加载配置"""
        self.server_input.setText(lower_config.server_url)
        self.device_id_input.setText(lower_config.device_id)
        self.device_name_input.setText(lower_config.device_name)

        if lower_config.hardware_key:
            self.hardware_key_label.setText(lower_config.hardware_key[:16] + "...")

    def save_current_config(self):
        """保存当前配置"""
        try:
            lower_config.server_url = self.server_input.text().strip()
            lower_config.device_id = self.device_id_input.text().strip()
            lower_config.device_name = self.device_name_input.text().strip()

            self.log("配置已保存到 lower_config.ini")
            QMessageBox.information(self, "成功", "配置已保存！")
        except Exception as e:
            self.log(f"保存配置失败: {e}", error=True)
            QMessageBox.critical(self, "错误", f"保存失败: {e}")

    def generate_hardware_key(self):
        """生成硬件密钥"""
        try:
            hardware_key = hardware_key_generator.get_machine_id()
            if hardware_key:
                lower_config.hardware_key = hardware_key
                self.hardware_key_label.setText(hardware_key[:16] + "...")
                self.log("硬件密钥生成成功")
                # 剪贴板
                QGuiApplication.clipboard().setText(hardware_key)
                QMessageBox.information(self, "成功", f"硬件密钥已生成\n{hardware_key},且已经复制到剪贴板")
            else:
                raise Exception("无法生成硬件密钥")
        except Exception as e:
            self.log(f"生成硬件密钥失败: {e}", error=True)
            QMessageBox.critical(self, "错误", f"生成失败: {e}")

    def register_device(self):
        """注册设备"""
        try:
            server_url = self.server_input.text().strip()
            device_id = self.device_id_input.text().strip()
            device_name = self.device_name_input.text().strip()
            hardware_key = lower_config.hardware_key

            if not all([server_url, device_id, device_name, hardware_key]):
                QMessageBox.warning(self, "警告", "请填写所有必要信息并生成硬件密钥")
                return

            # 保存配置
            lower_config.device_id = device_id
            lower_config.device_name = device_name

            # 创建客户端
            self.client = APIClient(server_url)

            # 获取本机IP
            device_ip = self.get_local_ip()

            # 注册设备
            result = self.client.register_device(device_id, device_name, hardware_key, device_ip)
            if result.get('message',None) is not None  :
                self.log(f"设备注册成功: {result.get('message')}")
                QMessageBox.information(self, "成功", "设备注册成功！")
            else:
                self.log(f"设备注册失败: {result.get('detail',None)}", error=True)
                QMessageBox.critical(self, "错误", f"注册失败: {result.get('detail',None)}")
        except Exception as e:
            self.log(f"设备注册失败: {e}", error=True)
            QMessageBox.critical(self, "错误", f"注册失败: {e}")

    def connect_to_server(self):
        """连接到服务器"""
        try:
            server_url = self.server_input.text().strip()
            device_id = self.device_id_input.text().strip()
            hardware_key = lower_config.hardware_key

            if not all([server_url, device_id, hardware_key]):
                QMessageBox.warning(self, "警告", "请填写必要信息")
                return

            # 创建客户端
            self.client = APIClient(server_url)

            # 获取本机IP
            device_ip = self.get_local_ip()

            # 认证设备
            result = self.client.authenticate_device(device_id, hardware_key, device_ip)

            self.authenticated = True
            self.log(f"连接成功: {result.get('message')}")
            
            # 根据模式选择连接方式
            if self.connection_mode == 'long_polling':
                # 使用HTTP长轮询心跳
                self.long_polling_thread = LowerLongPollingThread(
                    server_url=server_url,
                    device_id=device_id,
                    hardware_key=hardware_key
                )
                
                # 启动心跳线程
                self.long_polling_thread.start()
                self.log("正在启动HTTP心跳保活...")
                
                self.statusBar().showMessage("已连接 ✓ (HTTP长轮询)")
                
            else:
                # 使用WebSocket（向后兼容）
                self.ws_thread = DeviceWebSocketThread(
                    server_url=server_url,
                    device_id=device_id,
                    hardware_key=hardware_key
                )
                
                # 设置 WebSocket 回调
                self.ws_thread.set_connected_callback(self.on_connected)
                self.ws_thread.set_disconnected_callback(self.on_disconnected)
                self.ws_thread.set_error_callback(self.on_error)
                
                # 启动 WebSocket 线程
                self.ws_thread.start()
                self.log("正在建立 WebSocket 长连接...")
                
                self.statusBar().showMessage("已连接 ✓ (WebSocket)")

            # 启用上传功能
            self.add_excel_btn.setEnabled(True)
            self.add_image_btn.setEnabled(True)
            self.remove_file_btn.setEnabled(True)
            self.clear_files_btn.setEnabled(True)

            QMessageBox.information(self, "成功", "连接服务器成功！")

        except Exception as e:
            self.authenticated = False
            self.log(f"连接失败: {e}", error=True)
            self.statusBar().showMessage("连接失败 ✗")
            QMessageBox.critical(self, "错误", f"连接失败: {e}")

    def add_excel_files(self):
        """添加电量数据（Excel）"""
        start_dir = lower_config.last_excel_dir or ""

        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择电量数据文件",
            start_dir,
            "Excel文件 (*.xlsx *.xls)"
        )

        if file_paths:
            # 保存浏览目录
            lower_config.last_excel_dir = str(Path(file_paths[0]).parent)

            for file_path in file_paths:
                self.add_meter_data(Path(file_path), DataType.EXCEL)

    def add_image_files(self):
        """添加几何量数据（图片）"""
        start_dir = lower_config.last_image_dir or ""

        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择几何量数据文件",
            start_dir,
            "图片文件 (*.jpg *.jpeg *.png *.bmp)"
        )

        if file_paths:
            # 保存浏览目录
            lower_config.last_image_dir = str(Path(file_paths[0]).parent)

            for file_path in file_paths:
                self.add_meter_data(Path(file_path), DataType.IMAGE)

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
        data_widget = MeterDataListItem(meter_data)

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
        """移除选中的数据"""
        selected_items = self.data_list_widget.selectedItems()

        if not selected_items:
            QMessageBox.warning(self, "警告", "请选择要移除的数据")
            return

        for item in selected_items:
            row = self.data_list_widget.row(item)
            widget = self.data_list_widget.itemWidget(item)

            if widget:
                # 从字典中移除
                file_path = str(widget.meter_data.file_path)
                if file_path in self.data_items:
                    del self.data_items[file_path]

            self.data_list_widget.takeItem(row)

        self.update_data_count()
        self.log(f"移除了 {len(selected_items)} 个数据")

    def clear_data_list(self):
        """清空数据列表"""
        if self.data_items:
            reply = QMessageBox.question(
                self,
                "确认",
                f"确定要清空所有 {len(self.data_items)} 个数据吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )

            if reply == QMessageBox.StandardButton.Yes:
                self.data_list_widget.clear()
                self.data_items.clear()
                self.update_data_count()
                self.log("已清空数据列表")

    def select_all_data(self):
        """全选数据"""
        for file_info in self.data_items.values():
            widget = file_info['widget']
            if not widget.uploaded:
                widget.checkbox.setChecked(True)
        self.log("已全选数据")
    
    def deselect_all_data(self):
        """全不选数据"""
        for file_info in self.data_items.values():
            widget = file_info['widget']
            if not widget.uploaded:
                widget.checkbox.setChecked(False)
        self.log("已全不选数据")
    
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
        """上传选中的数据"""
        if not self.authenticated:
            QMessageBox.warning(self, "警告", "请先连接服务器")
            return

        if not self.data_items:
            QMessageBox.warning(self, "警告", "请先添加要上传的数据")
            return

        # 获取选中的数据
        selected_items = [
            file_info for file_info in self.data_items.values()
            if file_info['widget'].is_selected()
        ]
        
        if not selected_items:
            QMessageBox.warning(self, "警告", "请选择要上传的数据")
            return

        # 禁用按钮
        self.upload_btn.setEnabled(False)
        self.stop_upload_btn.setEnabled(True)
        self.add_excel_btn.setEnabled(False)
        self.add_image_btn.setEnabled(False)

        self.log(f"开始上传 {len(selected_items)} 个数据...")

        excel_count = sum(1 for item in selected_items if item['data'].is_excel)
        image_count = len(selected_items) - excel_count
        self.log(f"  - 电量数据: {excel_count} 个")
        self.log(f"  - 几何量数据: {image_count} 个")

        # 创建上传任务（仅上传选中的）
        for file_info in selected_items:
            data_widget = file_info['widget']
            meter_data = file_info['data']

            # 更新状态
            data_widget.set_status("上传中...", "blue")
            data_widget.set_progress(0)

            # 创建工作线程
            worker = UploadWorker(
                self.client,
                lower_config.device_id,
                lower_config.hardware_key,
                meter_data
            )

            # 连接信号
            worker.signals.progress.connect(self.on_upload_progress)
            worker.signals.finished.connect(self.on_upload_finished)

            # 添加到线程池
            self.thread_pool.start(worker)

    def stop_upload(self):
        """停止上传"""
        reply = QMessageBox.question(
            self,
            "确认",
            "确定要停止所有上传任务吗？\n（正在上传的数据会继续完成）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            self.thread_pool.clear()
            self.log("已停止上传任务")
            self.restore_upload_buttons()

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
                    file_info['widget'].set_status("✗ 失败", "red")
                    file_info['widget'].set_progress(0)
                    self.log(f"✗ {file_name}: {message}", error=True)
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

        # 关闭连接
        if self.ws_thread:
            self.log("正在断开 WebSocket 连接...")
            self.ws_thread.stop()
            self.ws_thread.join(timeout=3)
        
        if self.long_polling_thread:
            self.log("正在停止HTTP心跳...")
            self.long_polling_thread.stop()
            self.long_polling_thread.join(timeout=3)

        self.thread_pool.waitForDone(3000)
        event.accept()
    
    def on_connected(self):
        """连接成功回调"""
        self.log("[连接] 长连接已建立")
    
    def on_disconnected(self):
        """断开连接回调"""
        self.log("[连接] 连接已断开", error=True)
    
    def on_error(self, error: str):
        """错误回调"""
        self.log(f"[连接] 错误: {error}", error=True)

def quit_qt_application(window:LowerComputerWindow):
    """退出应用程序"""
    if window.authenticated:
        # 关闭 WebSocket 连接
        if window.ws_thread:
            logger.info("关闭 WebSocket 连接...")
            window.ws_thread.stop()
            window.ws_thread.join(timeout=3)
        
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