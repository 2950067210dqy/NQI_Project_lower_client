import configparser
import sys
from pathlib import Path
from datetime import datetime


class LowerConfig:
    def __init__(self):
        self.app_dir = self._app_dir()
        self.bundle_dir = self._bundle_dir()
        # 打包后 lower_config.ini 复制到 exe 同级目录，方便现场直接修改。
        self.config_file = self.app_dir / "lower_config.ini"
        self.config = configparser.ConfigParser()

        if not self.config_file.exists():
            self.create_default_config()

        self.config.read(self.config_file, encoding='utf-8')

    @staticmethod
    def _app_dir() -> Path:
        """返回 exe 所在目录；源码运行时返回当前工作目录。"""
        if getattr(sys, 'frozen', False):
            return Path(sys.executable).resolve().parent
        return Path.cwd()

    @staticmethod
    def _bundle_dir() -> Path:
        """返回 PyInstaller 资源目录；one-dir 下通常是 exe/_internal。"""
        if getattr(sys, 'frozen', False):
            return Path(getattr(sys, '_MEIPASS', Path(sys.executable).resolve().parent))
        return Path.cwd()

    def _resource_path(self, value: str) -> str:
        """解析资源路径，打包后优先读取 exe 同级目录的 static 资源。"""
        path = Path(value).expanduser()
        if path.is_absolute():
            return str(path)
        external_path = self.app_dir / path
        if external_path.exists():
            return str(external_path)
        bundled_path = self.bundle_dir / path
        if bundled_path.exists():
            return str(bundled_path)
        return str(external_path)

    def create_default_config(self):
        """创建默认配置"""
        self.config['server'] = {
            'url': 'http://localhost:8000',
            'timeout': '30'
        }

        self.config['device'] = {
            'device_id': '',
            'device_name': 'Three-Phase Meter Device',
            'hardware_key': '',
            'location': '长沙市'
        }

        self.config['upload'] = {
            'auto_retry': 'true',
            'max_retries': '3',
            'concurrent_uploads': '2'
        }

        self.config['path'] = {
            'last_excel_dir': '',
            'last_image_dir': ''
        }

        self.config['meter_data'] = {
            'excel_description_format': '电量数据_{timestamp}',
            'image_description_format': '几何量数据_{timestamp}'
        }

        self.config['local_storage'] = {
            'enable_local_cache': 'true',
            'cache_dir': './meter_data_cache'
        }

        with open(self.config_file, 'w', encoding='utf-8') as f:
            self.config.write(f)

    def save(self):
        """保存配置"""
        with open(self.config_file, 'w', encoding='utf-8') as f:
            self.config.write(f)

    def get(self, section, key, fallback=None):
        """获取配置值"""
        return self.config.get(section, key, fallback=fallback)

    def set(self, section, key, value):
        """设置配置值"""
        if not self.config.has_section(section):
            self.config.add_section(section)
        self.config.set(section, key, value)
        self.save()

    @property
    def ui_NQI_path(self):
        return self._resource_path(self.config.get('ui', 'NQI_path'))

    @ui_NQI_path.setter
    def ui_NQI_path(self, value):
        self.set('ui', 'NQI_path', value)

    @property
    def ui_school_path(self):
        return self._resource_path(self.config.get('ui', 'school_path'))

    @ui_school_path.setter
    def ui_school_path(self, value):
        self.set('ui', 'school_path', value)
    @property
    def server_url(self):
        return self.config.get('server', 'url')

    @server_url.setter
    def server_url(self, value):
        self.set('server', 'url', value)

    @property
    def server_url(self):
        return self.config.get('server', 'url')

    @server_url.setter
    def server_url(self, value):
        self.set('server', 'url', value)

    @property
    def device_id(self):
        return self.config.get('device', 'device_id')

    @device_id.setter
    def device_id(self, value):
        self.set('device', 'device_id', value)

    @property
    def device_name(self):
        return self.config.get('device', 'device_name')

    @device_name.setter
    def device_name(self, value):
        self.set('device', 'device_name', value)

    @property
    def hardware_key(self):
        return self.config.get('device', 'hardware_key')

    @hardware_key.setter
    def hardware_key(self, value):
        self.set('device', 'hardware_key', value)

    @property
    def location(self):
        return self.config.get('device', 'location', fallback='长沙市')

    @location.setter
    def location(self, value):
        self.set('device', 'location', value)

    @property
    def last_excel_dir(self):
        return self.config.get('path', 'last_excel_dir', fallback='')

    @last_excel_dir.setter
    def last_excel_dir(self, value):
        self.set('path', 'last_excel_dir', value)

    @property
    def last_image_dir(self):
        return self.config.get('path', 'last_image_dir', fallback='')

    @last_image_dir.setter
    def last_image_dir(self, value):
        self.set('path', 'last_image_dir', value)

    @property
    def concurrent_uploads(self):
        return self.config.getint('upload', 'concurrent_uploads', fallback=2)

    @property
    def enable_local_cache(self):
        return self.config.getboolean('local_storage', 'enable_local_cache', fallback=True)

    @property
    def cache_dir(self):
        return self.app_dir / self.config.get('local_storage', 'cache_dir', fallback='./meter_data_cache')

    def get_excel_description(self):
        """获取电量数据描述"""
        format_str = self.config.get('meter_data', 'excel_description_format',
                                     fallback='电量数据_{timestamp}')
        return format_str.format(timestamp=datetime.now().strftime("%Y%m%d_%H%M%S"))

    def get_image_description(self):
        """获取几何量数据描述"""
        format_str = self.config.get('meter_data', 'image_description_format',
                                     fallback='几何量数据_{timestamp}')
        return format_str.format(timestamp=datetime.now().strftime("%Y%m%d_%H%M%S"))


lower_config = LowerConfig()