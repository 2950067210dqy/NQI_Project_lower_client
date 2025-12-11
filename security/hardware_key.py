import uuid
import hashlib
import psutil


class HardwareKeyGenerator:
    """硬件密钥生成器"""

    @staticmethod
    def get_machine_id():
        """获取机器唯一标识"""
        try:
            # 获取MAC地址
            mac = ':'.join(['{:02x}'.format((uuid.getnode() >> elements) & 0xff)
                            for elements in range(0, 2 * 6, 2)][::-1])

            # 获取CPU信息
            cpu_info = f"{psutil.cpu_count()}"

            # 组合信息
            unique_info = f"{mac}-{cpu_info}"

            # 生成SHA256哈希
            hash_obj = hashlib.sha256(unique_info.encode())
            hardware_key = hash_obj.hexdigest()

            return hardware_key

        except Exception as e:
            print(f"Failed to generate hardware key: {e}")
            return None


hardware_key_generator = HardwareKeyGenerator()