"""独立本地部署控制器，只签发短时运行清单证明。"""

from opsswarm.deployment_controller.app import create_app

__all__ = ["create_app"]
