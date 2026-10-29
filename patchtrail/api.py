"""HTTP contract and operator boundary. Run exactly one API process."""
from collections import defaultdict, deque
from contextlib import asynccontextmanager
import hmac
from pathlib import Path
import threading
import time
import sqlite3
from typing import Literal

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .config import ROOT, Settings, load_local_env, runtime_code_hash
from .service import RunService, ServiceError, public_run
from .storage import RunNotFound, StoreConflict
from .tasks import list_tasks
from .workspace import WorkspaceError
from .web_imports import IMPORT_BODY_LIMIT, WebImportError, WebImportStore

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


class CreateRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(pattern=r"^[a-z0-9-]{3,64}$")
    mode: Literal["demo", "live"] = "demo"


class ApproveRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_version: int = Field(ge=0, strict=True)


class ResumeRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0, strict=True)


class UploadedSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(strict=True, min_length=1, max_length=512)
    content: str = Field(strict=True, max_length=128 * 1024)


class PreviewImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest: dict = Field(strict=True)
    files: list[UploadedSource] = Field(min_length=2, max_length=200)


class CommitImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_id: str = Field(strict=True, pattern=r"^upload-[a-f0-9]{64}$")


def create_app(service=None):
    load_local_env()
    owned = service is None
    active = service or RunService(Settings())

    @asynccontextmanager
    async def lifespan(app):
        yield
        if owned:
            active.close()

    app = FastAPI(title="PatchTrail", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url="/api/openapi.json", lifespan=lifespan)
    app.state.service = active
    imports = WebImportStore(active.settings.data_dir / "web-imports", active.catalog)
    app.state.web_imports = imports
    recent = defaultdict(deque)
    rate_lock = threading.Lock()

    def is_operator(request):
        configured = active.settings.admin_token
        supplied = request.headers.get("authorization", "")
        return bool(configured) and hmac.compare_digest(supplied.encode(), ("Bearer " + configured).encode())

    def operator(request):
        configured = active.settings.admin_token
        if not configured:
            raise ServiceError("operator_not_configured", "Operator token is not configured", 503)
        if not is_operator(request):
            raise ServiceError("operator_required", "Operator authentication is required", 401)

    def access_run(request, row):
        if row["mode"] == "live" or not active.settings.public_demo:
            operator(request)

    @app.middleware("http")
    async def headers(request, call_next):
        def finish(response):
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Content-Security-Policy"] = CSP
            if request.url.path.startswith("/api/"):
                response.headers["Cache-Control"] = "no-store"
            return response

        is_import = request.url.path.startswith("/api/imports/")
        # Authenticate imports before consuming a byte of the source request body.
        if is_import:
            try:
                operator(request)
            except ServiceError as exc:
                return finish(JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, status_code=exc.status))
            if request.headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                return finish(JSONResponse({"error": {"code": "import_invalid", "message": "Import uses uncompressed JSON text files only"}}, status_code=415))
            if request.method == "POST" and request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
                return finish(JSONResponse({"error": {"code": "import_invalid", "message": "Import requests require application/json"}}, status_code=415))
        limit = IMPORT_BODY_LIMIT if request.method == "POST" and request.url.path == "/api/imports/preview" else 4096
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            return finish(JSONResponse({"error": {"code": "invalid_length", "message": "Invalid request length"}}, status_code=400))
        if length < 0:
            return finish(JSONResponse({"error": {"code": "invalid_length", "message": "Invalid request length"}}, status_code=400))
        if length > limit:
            return finish(JSONResponse({"error": {"code": "request_too_large", "message": "Request exceeds this endpoint's body limit"}}, status_code=413))
        if request.method == "POST":
            parts, size = [], 0
            async for part in request.stream():
                size += len(part)
                if size > limit:
                    return finish(JSONResponse({"error": {"code": "request_too_large", "message": "Request exceeds this endpoint's body limit"}}, status_code=413))
                parts.append(part)
            # Starlette's fixed-version _CachedRequest replays this bounded body.
            request._body = b"".join(parts)
        response = await call_next(request)
        return finish(response)

    @app.exception_handler(WebImportError)
    async def invalid_web_import(request, exc):
        return JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, status_code=exc.status)

    @app.exception_handler(ServiceError)
    async def service_error(request, exc):
        return JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, status_code=exc.status)

    @app.exception_handler(StoreConflict)
    async def conflict(request, exc):
        return JSONResponse({"error": {"code": "state_conflict", "message": str(exc)}}, status_code=409)

    @app.exception_handler(RunNotFound)
    async def absent(request, exc):
        return JSONResponse({"error": {"code": "not_found", "message": "Run not found"}}, status_code=404)

    @app.exception_handler(WorkspaceError)
    async def invalid_project(request, exc):
        return JSONResponse({"error": {"code": "project_unavailable", "message": "Imported project snapshot validation failed"}}, status_code=503)

    @app.exception_handler(sqlite3.OperationalError)
    async def database_error(request, exc):
        busy = (getattr(exc, "sqlite_errorcode", 0) & 255) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
        if busy:
            return JSONResponse({"error": {"code": "storage_busy", "message":
                "Storage is temporarily busy; retry reads or reuse the original request key"}},
                status_code=503, headers={"Retry-After": "1"})
        return JSONResponse({"error": {"code": "storage_error", "message": "Storage operation failed"}}, status_code=500)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        # Do not echo arbitrary input (especially Authorization or operator values).
        return JSONResponse({"error": {"code": "invalid_request", "message": "Request fields do not match the API contract"}}, status_code=422)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": __version__, "model_configured": bool(active.settings.api_key),
                "runtime_code_hash": runtime_code_hash(),
                "model": active.settings.model, "operator_configured": bool(active.settings.admin_token),
                "container_available": active.runner.available(), "public_demo": active.settings.public_demo,
                "pytest_configured": bool(active.settings.pytest_image),
                "web_import_available": bool(active.settings.admin_token),
                "web_import_limits": {"max_files": 200, "max_file_bytes": 128 * 1024, "max_source_bytes": 2 * 1024 * 1024, "max_request_bytes": IMPORT_BODY_LIMIT},
                "demo_mode": "scripted provider; actual container verifier required",
                "scope": "public curated demo; operator-only bounded source-file previews and private snapshots; fixed tests; no archives, remote Git or shell"}

    @app.get("/api/tasks")
    def tasks(request: Request):
        return {"tasks": active.task_list(private=is_operator(request)),
                "evaluation_scope": "public authored fixtures and operator-only imported snapshots; not blind external benchmark"}

    @app.get("/api/projects")
    def projects(request: Request):
        operator(request)
        return {"projects": active.catalog.list_projects(), "import_method": "operator-only browser preview/commit or local manifest CLI"}

    def import_call(operation, *args):
        try:
            return operation(*args)
        except WebImportError:
            raise
        except (WorkspaceError, OSError, ValueError, TypeError):
            raise WebImportError("import_unavailable", "Import preview storage is unavailable; inspect it before retrying", 503) from None

    @app.post("/api/imports/preview")
    def preview_import(body: PreviewImport, request: Request):
        operator(request)
        return import_call(imports.preview, body.manifest, [item.model_dump() for item in body.files])

    @app.post("/api/imports/commit")
    def commit_import(body: CommitImport, request: Request):
        operator(request)
        return import_call(imports.commit, body.preview_id)

    @app.get("/api/imports/{preview_id}")
    def import_status(preview_id: str, request: Request):
        operator(request)
        return import_call(imports.get, preview_id)

    @app.get("/api/runs")
    def runs(request: Request):
        if not active.settings.public_demo:
            operator(request)
        rows = active.store.list_runs()
        if not is_operator(request):
            rows = [row for row in rows if row["mode"] == "demo"]
        return {"runs": [public_run(row) for row in rows]}

    @app.get("/api/runs/{run_id}")
    def run(run_id: str, request: Request):
        row = active.store.get_run(run_id)
        access_run(request, row)
        return public_run(row)

    @app.get("/api/runs/{run_id}/events")
    def events(run_id: str, request: Request, after: int = 0):
        access_run(request, active.store.get_run(run_id))
        if after < 0:
            raise ServiceError("invalid_cursor", "Event cursor cannot be negative")
        return {"events": active.store.events(run_id, after)}

    @app.get("/api/runs/{run_id}/report")
    def report(run_id: str, request: Request):
        row = active.store.get_run(run_id)
        access_run(request, row)
        return {"run": public_run(row), "events": active.store.events(run_id),
                "evidence_boundary": "scripted runtime checks and live model outcomes are separate; no claim of general repair accuracy"}

    @app.get("/api/runs/{run_id}/patch.diff", response_class=PlainTextResponse)
    def patch(run_id: str, request: Request):
        row = active.store.get_run(run_id)
        access_run(request, row)
        if not row.get("proposal"):
            raise ServiceError("patch_not_available", "This run has no patch proposal", 404)
        return PlainTextResponse(row["proposal"]["diff"], headers={
            "Content-Disposition": f'attachment; filename="patchtrail-{row["id"]}.diff"',
            "X-Proposal-Digest": row["proposal"]["digest"]})

    @app.post("/api/runs", status_code=201)
    def create(body: CreateRun, request: Request,
               idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128)):
        if body.mode == "live" or body.task_id.startswith("project-") or not active.settings.public_demo:
            operator(request)
        key = request.client.host if request.client else "local"
        # A retry of an existing key does not consume another public start allowance.
        if not active.store.find_request(idempotency_key):
            with rate_lock:
                now = time.monotonic()
                queue = recent[key]
                while queue and queue[0] < now - 60:
                    queue.popleft()
                if len(queue) >= 6:
                    raise ServiceError("rate_limited", "Please wait before starting another run", 429)
                queue.append(now)
        return public_run(active.create(body.task_id, body.mode, idempotency_key))

    @app.post("/api/runs/{run_id}/approve")
    def approve(run_id: str, body: ApproveRun, request: Request):
        access_run(request, active.store.get_run(run_id))
        return public_run(active.approve(run_id, body.proposal_digest, body.expected_version))

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str, request: Request):
        access_run(request, active.store.get_run(run_id))
        return public_run(active.cancel(run_id))

    @app.post("/api/runs/{run_id}/resume")
    def resume(run_id: str, body: ResumeRun, request: Request):
        access_run(request, active.store.get_run(run_id))
        return public_run(active.resume(run_id, body.expected_version))

    web = ROOT / "web"
    app.mount("/assets", StaticFiles(directory=web), name="assets")

    @app.get("/")
    def index():
        return FileResponse(web / "index.html")

    return app
