"""
下位机HTTP长轮询客户端
实现基于HTTP的心跳保活机制，适配不支持WebSocket的服务器环境。
"""
import requests
import threading
import time
from loguru import logger


class LowerLongPollingClient:
    """下位机HTTP长轮询客户端（用于保活和注册状态反馈）。"""

    def __init__(self, server_url: str, device_id: str, hardware_key: str, heartbeat_interval: int = 30, location: str = None):
        """
        初始化长轮询客户端。

        Args:
            server_url: 服务器地址（如 http://localhost:8000）
            device_id: 设备ID
            hardware_key: 硬件密钥
            heartbeat_interval: 心跳间隔（秒）
        """
        self.server_url = server_url.rstrip('/')
        self.device_id = device_id
        self.hardware_key = hardware_key
        self.location = location
        self.connected = False
        self.running = False

        # 回调由主界面注入，避免网络线程直接操作界面逻辑。
        self.on_connected_callback = None
        self.on_disconnected_callback = None
        self.on_error_callback = None
        self.on_registration_pending_callback = None

        # 服务器不支持WebSocket，在线状态统一通过HTTP心跳维持。
        self.heartbeat_interval = heartbeat_interval
        self.retry_interval = 5

        logger.info(f"[下位机长轮询] 初始化: {device_id}")

    def start(self):
        """开始心跳循环。"""
        self.running = True
        logger.info(f"[下位机长轮询] 开始心跳: {self.server_url}")

        while self.running:
            try:
                response = requests.post(
                    f"{self.server_url}/api/polling/heartbeat",
                    data={
                        'device_id': self.device_id,
                        'hardware_key': self.hardware_key,
                        'location': self.location
                    },
                    timeout=10
                )

                if response.status_code == 200:
                    if not self.connected:
                        self.connected = True
                        logger.info(f"[下位机长轮询] 连接成功: {self.device_id}")
                        self._emit_connected()
                    logger.debug("[下位机心跳] 成功")
                else:
                    logger.warning(f"[下位机心跳] 失败: {response.status_code}")
                    self._handle_failed_heartbeat(response)

            except requests.exceptions.Timeout:
                logger.warning("[下位机心跳] 超时")
                self._emit_error("心跳请求超时")
                self._mark_disconnected()

            except requests.exceptions.ConnectionError as e:
                logger.error(f"[下位机心跳] 连接错误: {e}")
                self._emit_error(f"连接错误: {e}")
                self._mark_disconnected()

            except Exception as e:
                logger.error(f"[下位机心跳] 错误: {e}")
                self._emit_error(str(e))
                self._mark_disconnected()

            if self.running:
                time.sleep(self.heartbeat_interval)

        self._send_offline_notice()
        self.connected = False
        logger.info("[下位机长轮询] 已停止")

    def stop(self):
        """停止心跳。"""
        self.running = False
        logger.info("[下位机长轮询] 停止中...")

    def is_connected(self) -> bool:
        """检查是否已连接。"""
        return self.connected and self.running

    def set_connected_callback(self, callback):
        """设置连接成功回调。"""
        self.on_connected_callback = callback

    def set_disconnected_callback(self, callback):
        """设置断开连接回调。"""
        self.on_disconnected_callback = callback

    def set_error_callback(self, callback):
        """设置错误回调。"""
        self.on_error_callback = callback

    def set_registration_pending_callback(self, callback):
        """设置注册待审批回调。"""
        self.on_registration_pending_callback = callback

    def _handle_failed_heartbeat(self, response):
        """根据心跳失败状态触发界面反馈。"""
        if response.status_code in (401, 403, 404):
            self._emit_registration_pending()
        else:
            self._emit_error(f"心跳失败: HTTP {response.status_code}")
        self._mark_disconnected()

    def _mark_disconnected(self):
        """只在状态从在线变为离线时触发断开回调。"""
        if self.connected and self.on_disconnected_callback:
            self.on_disconnected_callback()
        self.connected = False

    def _emit_connected(self):
        if self.on_connected_callback:
            self.on_connected_callback()

    def _emit_error(self, message: str):
        if self.on_error_callback:
            self.on_error_callback(message)

    def _emit_registration_pending(self):
        if self.on_registration_pending_callback:
            self.on_registration_pending_callback()
        else:
            self._emit_error("设备未注册或注册申请待审批")

    def _send_offline_notice(self):
        """停止线程时通知服务器设备离线；失败不影响客户端退出。"""
        try:
            requests.post(
                f"{self.server_url}/api/device/offline",
                data={
                    'device_id': self.device_id,
                    'hardware_key': self.hardware_key
                },
                timeout=5
            )
            logger.info("[下位机长轮询] 已发送离线通知")
        except Exception:
            pass


class LowerLongPollingThread(threading.Thread):
    """下位机HTTP长轮询运行线程。"""

    def __init__(self, server_url: str, device_id: str, hardware_key: str, poll_interval: int = 30, location: str = None):
        super().__init__(daemon=True)
        self.client = LowerLongPollingClient(
            server_url=server_url,
            device_id=device_id,
            hardware_key=hardware_key,
            heartbeat_interval=poll_interval,
            location=location
        )
        self.running = True

    def run(self):
        """运行线程。"""
        try:
            self.client.start()
        except Exception as e:
            logger.error(f"[下位机长轮询线程] 错误: {e}")

    def stop(self):
        """停止线程。"""
        self.running = False
        self.client.stop()

    def join(self, timeout=None):
        """等待线程结束。"""
        self.stop()
        super().join(timeout)

    def is_connected(self) -> bool:
        """检查是否已连接。"""
        return self.client.is_connected()

    def set_connected_callback(self, callback):
        """设置连接成功回调。"""
        self.client.set_connected_callback(callback)

    def set_disconnected_callback(self, callback):
        """设置断开连接回调。"""
        self.client.set_disconnected_callback(callback)

    def set_error_callback(self, callback):
        """设置错误回调。"""
        self.client.set_error_callback(callback)

    def set_registration_pending_callback(self, callback):
        """设置注册待审批回调。"""
        self.client.set_registration_pending_callback(callback)