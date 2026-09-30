"""运行期配置：全部可通过环境变量覆盖。"""
from __future__ import annotations

import os
from functools import lru_cache


class Settings:
    def __init__(self) -> None:
        # SQLite 数据库文件路径，容器内默认 /data/trace.db
        self.db_path: str = os.getenv("TRACE_DB_PATH", "./data/trace.db")
        # 监听地址 / 端口
        self.host: str = os.getenv("TRACE_HOST", "0.0.0.0")
        self.port: int = int(os.getenv("TRACE_PORT", "8080"))
        # SQLite busy timeout（毫秒）
        self.busy_timeout_ms: int = int(os.getenv("TRACE_BUSY_TIMEOUT_MS", "5000"))
        # 列表接口单页最大条数
        self.max_page_size: int = int(os.getenv("TRACE_MAX_PAGE_SIZE", "200"))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """测试辅助：重新读取环境变量。"""
    get_settings.cache_clear()
