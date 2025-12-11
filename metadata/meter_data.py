"""
三相表数据管理模块
处理电量数据（Excel）和几何量数据（图片）
"""
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, List
from dataclasses import dataclass
from datetime import datetime
from loguru import logger
import json


class DataType(Enum):
    """数据类型枚举"""
    EXCEL = "excel"  # 电量数据
    IMAGE = "image"  # 几何量数据


@dataclass
class MeterData:
    """三相表数据类"""
    data_type: DataType
    file_path: Path
    timestamp: datetime
    description: str
    file_size: int = 0

    def __post_init__(self):
        """初始化后处理"""
        if self.file_path.exists():
            self.file_size = self.file_path.stat().st_size

    @property
    def is_excel(self) -> bool:
        """是否为电量数据"""
        return self.data_type == DataType.EXCEL

    @property
    def is_image(self) -> bool:
        """是否为几何量数据"""
        return self.data_type == DataType.IMAGE

    def to_dict(self) -> Dict:
        """转换为字典"""
        return {
            'data_type': self.data_type.value,
            'file_name': self.file_path.name,
            'file_path': str(self.file_path),
            'timestamp': self.timestamp.isoformat(),
            'description': self.description,
            'file_size': self.file_size
        }


class MeterDataManager:
    """三相表数据管理器"""

    EXCEL_EXTENSIONS = {'.xlsx', '.xls'}
    IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.bmp'}

    def __init__(self, cache_dir: Optional[Path] = None):
        """初始化管理器"""
        self.cache_dir = cache_dir or Path('./meter_data_cache')
        self._init_cache_dir()

    def _init_cache_dir(self):
        """初始化缓存目录"""
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # 创建子目录
        self.excel_cache_dir = self.cache_dir / 'excel'
        self.image_cache_dir = self.cache_dir / 'image'

        self.excel_cache_dir.mkdir(exist_ok=True)
        self.image_cache_dir.mkdir(exist_ok=True)

    @staticmethod
    def detect_data_type(file_path: Path) -> Optional[DataType]:
        """检测数据类型"""
        ext = file_path.suffix.lower()

        if ext in MeterDataManager.EXCEL_EXTENSIONS:
            return DataType.EXCEL
        elif ext in MeterDataManager.IMAGE_EXTENSIONS:
            return DataType.IMAGE
        else:
            return None

    def create_meter_data(self, file_path: Path, description: str = None) -> Optional[MeterData]:
        """创建三相表数据对象"""
        if not file_path.exists():
            logger.error(f"文件不存在: {file_path}")
            return None

        data_type = self.detect_data_type(file_path)
        if data_type is None:
            logger.error(f"不支持的文件类型: {file_path.suffix}")
            return None

        if description is None:
            if data_type == DataType.EXCEL:
                description = f"电量数据 - {file_path.stem}"
            else:
                description = f"几何量数据 - {file_path.stem}"

        return MeterData(
            data_type=data_type,
            file_path=file_path,
            timestamp=datetime.now(),
            description=description
        )

    # def save_to_cache(self, meter_data: MeterData) -> Path:
    #     """保存数据到本地缓存"""
    #     if meter_data.is_excel:
    #         cache_dir = self.excel_cache_dir
    #     else:
    #         cache_dir = self.image_cache_dir
    #
    #     # 生成缓存文件名
    #     timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    #     cache_file = cache_dir / f"{timestamp}_{meter_data.file_path.name}"
    #
    #     # 复制文件到缓存
    #     import shutil
    #     shutil.copy2(meter_data.file_path, cache_file)
    #
    #     logger.info(f"数据已缓存: {cache_file}")
    #     return cache_file
    #
    # def get_cache_info(self) -> Dict:
    #     """获取缓存信息"""
    #     excel_files = list(self.excel_cache_dir.glob('*'))
    #     image_files = list(self.image_cache_dir.glob('*'))
    #
    #     excel_size = sum(f.stat().st_size for f in excel_files if f.is_file())
    #     image_size = sum(f.stat().st_size for f in image_files if f.is_file())
    #     result = {
    #         'excel_count': len(excel_files),
    #         'excel_size': excel_size,
    #         'image_count': len(image_files),
    #         'image_size': image_size,
    #         'total_count': len(excel_files) + len(image_files),
    #         'total_size': excel_size + image_size
    #     }
    #     logger.info(f"缓存：{result}")
    #     return result
    #
    # def clear_cache(self, data_type: Optional[DataType] = None):
    #     """清除缓存"""
    #     import shutil
    #
    #     if data_type is None:
    #         # 清除所有缓存
    #         shutil.rmtree(self.cache_dir)
    #         self._init_cache_dir()
    #         logger.info("已清除所有缓存")
    #     elif data_type == DataType.EXCEL:
    #         # 清除电量数据缓存
    #         for file in self.excel_cache_dir.glob('*'):
    #             if file.is_file():
    #                 file.unlink()
    #         logger.info("已清除电量数据缓存")
    #     elif data_type == DataType.IMAGE:
    #         # 清除几何量数据缓存
    #         for file in self.image_cache_dir.glob('*'):
    #             if file.is_file():
    #                 file.unlink()
    #         logger.info("已清除几何量数据缓存")


meter_data_manager = MeterDataManager()