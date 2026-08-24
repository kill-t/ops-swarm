"""Uvicorn 使用的 OpsSwarm 控制面入口。"""

from opsswarm.control_plane.app import create_app

app = create_app()
