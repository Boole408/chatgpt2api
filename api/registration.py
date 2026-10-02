from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from api.support import require_admin
from services.auto_registration_service import auto_registration_service
from services.registration_mailbox import RegistrationError, parse_emails


class SettingsRequest(BaseModel):
    credential_line: str = Field(default="", max_length=16000)
    protocol: str = "imap"
    registration_password: str | None = Field(default=None, max_length=256)
    registration_driver: str | None = None
    roxy_api_base: str | None = Field(default=None, max_length=256)
    roxy_api_token: str | None = Field(default=None, max_length=4000)
    roxy_workspace_id: str | None = Field(default=None, max_length=100)
    roxy_project_id: str | None = Field(default=None, max_length=100)


class EmailRequest(BaseModel):
    content: str = Field(max_length=200000)


class ManualActionRequest(BaseModel):
    action: str


async def call(function, *args, **kwargs):
    try:
        return await run_in_threadpool(function, *args, **kwargs)
    except RegistrationError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)}) from None


def create_router() -> APIRouter:
    router = APIRouter(prefix="/api/accounts/registration")

    @router.get("/settings")
    async def settings(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return auto_registration_service.settings.public()

    @router.post("/settings")
    async def save_settings(body: SettingsRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await call(auto_registration_service.save_settings, **body.model_dump())

    @router.post("/test-mailbox")
    async def test_mailbox(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await call(auto_registration_service.test_mailbox)

    @router.post("/test-roxy")
    async def test_roxy(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await call(auto_registration_service.test_roxy)

    @router.post("/preview")
    async def preview(body: EmailRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return parse_emails(body.content)

    @router.get("/jobs")
    async def jobs(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return auto_registration_service.list_jobs()

    @router.post("/jobs")
    async def create_job(body: EmailRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await call(auto_registration_service.create, body.content)

    @router.get("/jobs/{job_id}")
    async def get_job(job_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await call(auto_registration_service.get, job_id)

    @router.post("/jobs/{job_id}/stop")
    async def stop_job(job_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await call(auto_registration_service.stop, job_id)

    @router.post("/jobs/{job_id}/retry")
    async def retry_job(job_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await call(auto_registration_service.retry, job_id)

    @router.post("/jobs/{job_id}/rows/{row_id}/manual")
    async def manual_action(job_id: str, row_id: str, body: ManualActionRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await call(auto_registration_service.manual_action, job_id, row_id, body.action)

    @router.get("/jobs/{job_id}/rows/{row_id}/diagnostic-image")
    async def diagnostic_image(job_id: str, row_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        path = await call(auto_registration_service.diagnostic_image, job_id, row_id)
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})

    return router
