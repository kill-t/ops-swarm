"""经验候选 HTTP 入口只负责可信请求、持久任务提交和查询。"""

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from opsswarm.experience.records import CandidateView, JobView
from opsswarm.experience.store import ExperienceStore
from opsswarm.identity.models import OperatorPrincipal


class CreateExperienceJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    expected_report_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class CreateExperienceJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    created: bool
    job: JobView


def get_experience_store(request: Request) -> ExperienceStore:
    store = getattr(request.app.state, "experience_store", None)
    if not isinstance(store, ExperienceStore):
        raise RuntimeError("ExperienceStore 尚未初始化")
    return store


StoreDependency = Annotated[ExperienceStore, Depends(get_experience_store)]


def create_experience_router() -> APIRouter:
    router = APIRouter(tags=["经验候选"])

    @router.post(
        "/api/v1/incident-reports/{report_id}/experience-generation-jobs",
        status_code=201,
        responses={200: {"model": CreateExperienceJobResponse}},
    )
    async def create_experience_job(
        report_id: UUID,
        body: CreateExperienceJobRequest,
        request: Request,
        response: Response,
        store: StoreDependency,
    ) -> CreateExperienceJobResponse:
        job, created = await store.create(
            report_id,
            body.expected_report_content_hash,
            principal=cast(OperatorPrincipal, request.state.operator_principal),
            authorization_id=cast(UUID, request.state.authorization_id),
        )
        if not created:
            response.status_code = 200
        return CreateExperienceJobResponse(created=created, job=job)

    @router.get("/api/v1/experience-generation-jobs/{job_id}")
    async def get_experience_job(job_id: UUID, store: StoreDependency) -> JobView:
        return await store.get(job_id)

    @router.get("/api/v1/experience-candidates/{candidate_id}")
    async def get_experience_candidate(candidate_id: UUID, store: StoreDependency) -> CandidateView:
        return await store.get_candidate(candidate_id)

    return router
