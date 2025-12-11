import asyncio
import websockets
from typing import Callable, Optional
from loguru import logger
from datetime import datetime
import threading


class DeviceWebSocketClient:
    """下位机 WebSocket 客户端 - 保持与服务端的长连接"""

    def __init__(self, server_url: str, device_id: str, hardware_key: str):
        """
        初始化 WebSocket 客户端

        Args:
            server_url: 服务器地址（如 http://localhost:8000）
            device_id: 设备ID
            hardware_key: 硬件密钥
        """
        # 转换 http/https 为 ws/wss
        self.ws_url = server_url.replace("http://", "ws://").replace("https://", "wss://")
        self.device_id = device_id
        self.hardware_key = hardware_key
        self.websocket = None
        self.connected = False
        self.running = False

        # 回调
        self.on_connected: Optional[Callable] = None
        self.on_disconnected: Optional[Callable] = None
        self.on_error: Optional[Callable] = None

        # 心跳相关
        self.last_heartbeat = datetime.now()

    async def connect(self):
        """连接到 WebSocket 服务器"""
        try:
            # 构建 WebSocket URL
            ws_endpoint = f"{self.ws_url}/ws/device/{self.device_id}?hardware_key={self.hardware_key}"

            logger.info(f"[WebSocket] 正在连接: {ws_endpoint}")

            async with websockets.connect(ws_endpoint) as websocket:
                self.websocket = websocket
                self.connected = True
                self.running = True

                logger.info(f"[WebSocket] 设备已连接: {self.device_id}")

                # 调用连接成功回调
                if self.on_connected:
                    self.on_connected()

                # 接收消息循环
                await self._receive_messages()

        except websockets.exceptions.ConnectionClosed:
            logger.warning("[WebSocket] 连接已关闭")
            self.connected = False
        except asyncio.CancelledError:
            logger.info("[WebSocket] 连接被取消")
            self.connected = False
        except Exception as e:
            logger.error(f"[WebSocket] 连接错误: {e}")
            self.connected = False
            if self.on_error:
                self.on_error(str(e))
        finally:
            self.websocket = None
            if self.on_disconnected:
                self.on_disconnected()

    async def _receive_messages(self):
        """接收消息"""
        try:
            while self.running and self.websocket:
                try:
                    message = await asyncio.wait_for(
                        self.websocket.recv(),
                        timeout=60  # 60秒超时
                    )

                    # 服务端发送的 "pong" 响应
                    if message.strip().lower() == "pong":
                        self.last_heartbeat = datetime.now()
                        logger.debug("[WebSocket] 心跳响应收到")
                        continue

                    # 其他消息
                    logger.info(f"[WebSocket] 收到消息: {message}")

                except asyncio.TimeoutError:
                    # 发送心跳
                    try:
                        await self._send_ping()
                    except Exception as e:
                        logger.error(f"[WebSocket] 发送心跳失败: {e}")
                        break

        except Exception as e:
            logger.error(f"[WebSocket] 接收消息错误: {e}")
            if self.on_error:
                self.on_error(str(e))

    async def _send_ping(self):
        """发送心跳"""
        if self.websocket and self.connected:
            try:
                await self.websocket.send("ping")
                logger.debug("[WebSocket] 心跳已发送")
            except Exception as e:
                logger.error(f"[WebSocket] 发送心跳失败: {e}")
                self.connected = False

    async def disconnect(self):
        """断开连接"""
        self.running = False

        if self.websocket:
            try:
                await self.websocket.close()
            except:
                pass

        self.websocket = None
        self.connected = False
        logger.info(f"[WebSocket] 连接已断开: {self.device_id}")

    def is_connected(self) -> bool:
        """检查是否已连接"""
        return self.connected and self.websocket is not None


class DeviceWebSocketThread(threading.Thread):
    """WebSocket 运行线程"""

    def __init__(self, server_url: str, device_id: str, hardware_key: str):
        super().__init__(daemon=True)
        self.client = DeviceWebSocketClient(server_url, device_id, hardware_key)
        self.loop = None
        self.running = True

    def run(self):
        """运行线程"""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        try:
            self.loop.run_until_complete(self.client.connect())
        except Exception as e:
            logger.error(f"[WebSocket] 线程错误: {e}")
        finally:
            self.loop.close()

    def stop(self):
        """停止线程"""
        self.running = False

        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self.client.disconnect(),
                self.loop
            )

    def set_connected_callback(self, callback: Callable):
        """设置连接成功回调"""
        self.client.on_connected = callback

    def set_disconnected_callback(self, callback: Callable):
        """设置断开连接回调"""
        self.client.on_disconnected = callback

    def set_error_callback(self, callback: Callable):
        """设置错误回调"""
        self.client.on_error = callback

    def is_connected(self) -> bool:
        """检查是否已连接"""
        return self.client.is_connected()

