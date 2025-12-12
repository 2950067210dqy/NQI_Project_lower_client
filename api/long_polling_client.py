"""
下位机HTTP长轮询客户端 - 替代WebSocket
实现基于HTTP的心跳保活机制
"""
import requests
import threading
import time
from typing import Optional
from loguru import logger
from datetime import datetime


class LowerLongPollingClient:
    """下位机HTTP长轮询客户端（用于保活）"""
    
    def __init__(self, server_url: str, device_id: str, hardware_key: str):
        """
        初始化长轮询客户端
        
        Args:
            server_url: 服务器地址（如 http://localhost:8000）
            device_id: 设备ID
            hardware_key: 硬件密钥
        """
        self.server_url = server_url.rstrip('/')
        self.device_id = device_id
        self.hardware_key = hardware_key
        self.connected = False
        self.running = False
        
        # 配置
        self.heartbeat_interval = 30  # 心跳间隔（秒）
        self.retry_interval = 5  # 重试间隔（秒）
        
        logger.info(f"[下位机长轮询] 初始化: {device_id}")
    
    def start(self):
        """开始心跳"""
        self.running = True
        
        logger.info(f"[下位机长轮询] 开始心跳: {self.server_url}")
        
        while self.running:
            try:
                # 发送心跳请求
                response = requests.post(
                    f"{self.server_url}/api/polling/heartbeat",
                    data={
                        'device_id': self.device_id,
                        'hardware_key': self.hardware_key
                    },
                    timeout=10
                )
                
                if response.status_code == 200:
                    if not self.connected:
                        self.connected = True
                        logger.info(f"[下位机长轮询] 连接成功: {self.device_id}")
                    
                    logger.debug(f"[下位机心跳] 成功")
                else:
                    logger.warning(f"[下位机心跳] 失败: {response.status_code}")
                    self.connected = False
                    
            except requests.exceptions.Timeout:
                logger.warning(f"[下位机心跳] 超时")
                self.connected = False
                
            except requests.exceptions.ConnectionError as e:
                logger.error(f"[下位机心跳] 连接错误: {e}")
                self.connected = False
                
            except Exception as e:
                logger.error(f"[下位机心跳] 错误: {e}")
                self.connected = False
            
            # 等待下一次心跳
            if self.running:
                time.sleep(self.heartbeat_interval)
        
        # 发送离线通知
        try:
            requests.post(
                f"{self.server_url}/api/device/offline",
                data={
                    'device_id': self.device_id,
                    'hardware_key': self.hardware_key
                },
                timeout=5
            )
            logger.info(f"[下位机长轮询] 已发送离线通知")
        except:
            pass
        
        self.connected = False
        logger.info(f"[下位机长轮询] 已停止")
    
    def stop(self):
        """停止心跳"""
        self.running = False
        logger.info(f"[下位机长轮询] 停止中...")
    
    def is_connected(self) -> bool:
        """检查是否已连接"""
        return self.connected and self.running


class LowerLongPollingThread(threading.Thread):
    """下位机HTTP长轮询运行线程"""
    
    def __init__(self, server_url: str, device_id: str, hardware_key: str):
        super().__init__(daemon=True)
        self.client = LowerLongPollingClient(server_url, device_id, hardware_key)
        self.running = True
    
    def run(self):
        """运行线程"""
        try:
            self.client.start()
        except Exception as e:
            logger.error(f"[下位机长轮询线程] 错误: {e}")
    
    def stop(self):
        """停止线程"""
        self.running = False
        self.client.stop()
    
    def join(self, timeout=None):
        """等待线程结束"""
        self.stop()
        super().join(timeout)
    
    def is_connected(self) -> bool:
        """检查是否已连接"""
        return self.client.is_connected()

