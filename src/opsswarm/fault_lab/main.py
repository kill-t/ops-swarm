"""Uvicorn 使用的故障演练服务入口。"""

from opsswarm.fault_lab.app import create_app

app = create_app()
