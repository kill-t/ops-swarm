"""候选执行恢复验证只读查询，不在HTTP请求内探测业务端点。"""

from uuid import UUID

from fastapi import APIRouter, Request, Response

from opsswarm.execution.release_jobs.api import VersionPath
from opsswarm.execution.release_jobs.contracts import ROUTE
from opsswarm.verification.candidate.contracts import RecoveryError, RecoveryView
from opsswarm.verification.candidate.store import CandidateRecoveryStore


def create_candidate_recovery_router() -> APIRouter:
    router = APIRouter(prefix=ROUTE + "/recovery-verification", tags=["候选恢复验证"])

    @router.get("")
    async def get_candidate_recovery_verification(
        run_id: UUID,
        job_id: UUID,
        version: VersionPath,
        request: Request,
        response: Response,
    ) -> RecoveryView:
        store = getattr(request.app.state, "candidate_recovery_store", None)
        if not isinstance(store, CandidateRecoveryStore):
            raise RecoveryError("recovery_unavailable", "恢复验证存储未初始化", 503)
        response.headers["Cache-Control"] = "no-store"
        return await store.get(run_id, job_id, int(version))

    return router
