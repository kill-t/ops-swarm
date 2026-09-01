"""Uvicorn 使用的独立部署控制器入口。"""

from opsswarm.deployment_controller.app import create_app

app = create_app()
