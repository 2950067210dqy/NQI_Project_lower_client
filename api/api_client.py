import requests
from typing import Optional, Dict
from pathlib import Path
from loguru import logger


class APIClient:
    """API客户端"""

    def __init__(self, base_url: str, timeout: int = 30):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.session = requests.Session()

    def register_device(self, device_id: str, device_name: str,
                        hardware_key: str, device_ip: str = None) -> Dict:
        """注册设备"""
        try:
            url = f"{self.base_url}/api/device/register"
            data = {
                'device_id': device_id,
                'device_name': device_name,
                'hardware_key': hardware_key,
                'device_ip': device_ip
            }

            response = self.session.post(url, data=data, timeout=self.timeout)
            # response.raise_for_status()

            return response.json()

        except Exception as e:
            logger.error(f"Device registration failed: {e}")
            raise

    def authenticate_device(self, device_id: str, hardware_key: str,
                            device_ip: str = None) -> Dict:
        """设备认证"""
        try:
            url = f"{self.base_url}/api/device/authenticate"
            data = {
                'device_id': device_id,
                'hardware_key': hardware_key,
                'device_ip': device_ip
            }

            response = self.session.post(url, data=data, timeout=self.timeout)
            response.raise_for_status()

            return response.json()

        except Exception as e:
            logger.error(f"Device authentication failed: {e}")
            raise

    def upload_file(self, device_id: str, hardware_key: str,
                    file_path: Path, description: str = None,
                    meter_model: str = None, meter_sn: str = None,
                    image_type: str = None) -> Dict:
        """
        上传文件（通用接口）
        根据文件类型自动识别为电量数据或几何量数据
        """
        try:
            url = f"{self.base_url}/api/upload/file"

            data = {
                'device_id': device_id,
                'hardware_key': hardware_key,
                'description': description or ''
            }

            # 添加可选参数
            if meter_model:
                data['meter_model'] = meter_model
            if meter_sn:
                data['meter_sn'] = meter_sn
            if image_type:
                data['image_type'] = image_type

            with open(file_path, 'rb') as f:
                files = {'file': (file_path.name, f)}
                response = self.session.post(url, data=data, files=files, timeout=self.timeout)

            response.raise_for_status()
            return response.json()

        except Exception as e:
            logger.error(f"File upload failed: {e}")
            raise

    def upload_excel(self, device_id: str, hardware_key: str,
                     file_path: Path, description: str = None,
                     meter_model: str = None, meter_sn: str = None) -> Dict:
        """上传电量数据（Excel）"""
        try:
            url = f"{self.base_url}/api/upload/excel"

            data = {
                'device_id': device_id,
                'hardware_key': hardware_key,
                'description': description or ''
            }

            if meter_model:
                data['meter_model'] = meter_model
            if meter_sn:
                data['meter_sn'] = meter_sn

            with open(file_path, 'rb') as f:
                files = {'file': (file_path.name, f)}
                response = self.session.post(url, data=data, files=files, timeout=self.timeout)

            response.raise_for_status()
            return response.json()

        except Exception as e:
            logger.error(f"Excel upload failed: {e}")
            raise

    def upload_image(self, device_id: str, hardware_key: str,
                     file_path: Path, description: str = None,
                     image_type: str = None) -> Dict:
        """上传几何量数据（图片）"""
        try:
            url = f"{self.base_url}/api/upload/image"

            data = {
                'device_id': device_id,
                'hardware_key': hardware_key,
                'description': description or ''
            }

            if image_type:
                data['image_type'] = image_type

            with open(file_path, 'rb') as f:
                files = {'file': (file_path.name, f)}
                response = self.session.post(url, data=data, files=files, timeout=self.timeout)

            response.raise_for_status()
            return response.json()

        except Exception as e:
            logger.error(f"Image upload failed: {e}")
            raise

    def set_device_status(self, device_id: str, hardware_key: str, status: str) -> Dict:
        """设置设备状态"""
        try:
            url = f"{self.base_url}/api/device/set-status"
            data = {
                'device_id': device_id,
                'hardware_key': hardware_key,
                'status': status
            }

            response = self.session.post(url, data=data, timeout=self.timeout)
            response.raise_for_status()

            return response.json()

        except Exception as e:
            logger.error(f"Failed to set device status: {e}")
            raise

    def set_device_offline(self, device_id: str, hardware_key: str) -> Dict:
        """设置设备离线"""
        try:
            url = f"{self.base_url}/api/device/offline"
            data = {
                'device_id': device_id,
                'hardware_key': hardware_key
            }

            response = self.session.post(url, data=data, timeout=self.timeout)
            response.raise_for_status()

            return response.json()

        except Exception as e:
            logger.error(f"Failed to set device offline: {e}")
            raise