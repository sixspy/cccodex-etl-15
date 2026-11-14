import os
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeSerializer

from app import idempotency
from app.config import Config
from app.db import get_db, get_pipeline, init_db, list_pipelines, run_extract

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
serializer = URLSafeSerializer(Config.SECRET, salt="cc-etl-session")

app = FastAPI(title="cc-etl-lab")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def _read_session(request: Request) -> dict:
    raw = request.cookies.get("etl_session")
    if not raw:
        return {}
    try:
        data = serializer.loads(raw)
        return data if isinstance(data, dict) else {}
    except BadSignature:
        return {}


def require_user(request: Request) -> str:
    user = _read_session(request).get("user")
    if user not in Config.USERS:
        raise idempotency.ApiError(401, "UNAUTHENTICATED", "未登录")
    return user


@app.exception_handler(idempotency.ApiError)
async def _api_error_handler(request: Request, exc: idempotency.ApiError):
    body = {
        "ok": False,
        "code": exc.code,
        "error": exc.message,
        "message": exc.message,
        **exc.extra,
    }
    return JSONResponse(body, status_code=exc.status)


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/")
def root(request: Request):
    if _read_session(request).get("user") in Config.USERS:
        return RedirectResponse("/app/", status_code=302)
    return RedirectResponse("/login", status_code=302)


@app.get("/login")
def login_page():
    return FileResponse(STATIC / "login.html")


@app.get("/app/")
def spa_page(request: Request):
    if _read_session(request).get("user") not in Config.USERS:
        return RedirectResponse("/login", status_code=302)
    return FileResponse(STATIC / "index.html")


@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    user = str(body.get("username", "")).strip()
    pwd = str(body.get("password", ""))
    row = Config.USERS.get(user)
    if not row or row[0] != pwd:
        return JSONResponse({"ok": False, "error": "账号或密码错误"}, status_code=400)
    resp = JSONResponse({"ok": True, "user": user, "role": row[1]})
    resp.set_cookie(
        "etl_session",
        serializer.dumps({"user": user}),
        httponly=True,
        samesite="lax",
    )
    return resp


@app.post("/api/logout")
def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("etl_session")
    return resp


@app.get("/api/me")
def api_me(user: str = Depends(require_user)):
    return {
        "user": user,
        "role": Config.role_of(user),
        "lab": Config.LAB_NAME,
        "domain": "数据工程",
        "users": [{"user": u, "role": r[1]} for u, r in Config.USERS.items()],
    }


@app.get("/api/overview")
def api_overview(user: str = Depends(require_user)):
    return idempotency.overview()


@app.get("/api/idempotency/policy")
def api_policy(user: str = Depends(require_user)):
    with get_db() as conn:
        return idempotency.policy_doc(conn)


@app.post("/api/idempotency/policy")
async def api_set_policy(request: Request, user: str = Depends(require_user)):
    body = await request.json()
    return idempotency.set_policy(
        user,
        str(body.get("policy", "")),
        str(body.get("description", "")),
    )


@app.get("/api/idempotency/keys")
def api_keys(user: str = Depends(require_user)):
    return {"items": idempotency.list_keys()}


@app.get("/api/rollbacks")
def api_rollbacks(user: str = Depends(require_user)):
    return {"items": idempotency.list_rollbacks()}


@app.get("/api/pipelines")
def api_pipelines(user: str = Depends(require_user)):
    with get_db() as conn:
        return {"items": list_pipelines(conn)}


@app.get("/api/pipelines/{pipeline_id}")
def api_pipeline_detail(pipeline_id: int, user: str = Depends(require_user)):
    with get_db() as conn:
        item = get_pipeline(conn, pipeline_id)
    if not item:
        raise idempotency.ApiError(404, "PIPELINE_NOT_FOUND", "管道不存在")
    item["ledger"] = idempotency.pipeline_ledger(pipeline_id)
    return item


@app.post("/api/pipelines/{pipeline_id}/extract")
def api_extract(pipeline_id: int, user: str = Depends(require_user)):
    with get_db() as conn:
        item = run_extract(conn, pipeline_id)
    if not item:
        raise idempotency.ApiError(404, "PIPELINE_NOT_FOUND", "管道不存在")
    return item


@app.post("/api/pipelines/{pipeline_id}/load")
async def api_load(pipeline_id: int, request: Request, user: str = Depends(require_user)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return idempotency.perform_load(
        pipeline_id,
        str(body.get("idem_key", "")),
        body.get("policy"),
        user,
        body.get("policy_description"),
    )


@app.post("/api/pipelines/{pipeline_id}/rollback")
async def api_rollback(pipeline_id: int, request: Request, user: str = Depends(require_user)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return idempotency.perform_rollback(
        pipeline_id,
        str(body.get("reason", "")),
        user,
    )


def create_app() -> FastAPI:
    return app
