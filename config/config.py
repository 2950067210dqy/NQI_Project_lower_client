import configparser
from pathlib import Path
from datetime import datetime


class LowerConfig:
    def __init__(self):
        self.config_file = Path("lower_config.ini")
        self.config = configparser.ConfigParser()

        if not self.config_file.exists():
            self.create_default_config()

        self.config.read(self.config_file, encoding='utf-8')

    def create_default_config(self):
        """创建默认配置"""
        self.config['server'] = {
            'url': 'http://localhost:8000',
            'timeout': '30'
        }

        self.config['device'] = {
            'device_id': '',
            'device_name': 'Three-Phase Meter Device',
            'hardware_key': ''
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
        return Path(self.config.get('local_storage', 'cache_dir', fallback='./meter_data_cache'))

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