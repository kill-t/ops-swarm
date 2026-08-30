"""Uvicorn 使用的变更记录模拟服务入口。"""

from opsswarm.change_records.app import create_app

app = create_app()
