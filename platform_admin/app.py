"""Single session-authenticated UI and internal browser API, under /admin."""

import hmac
import json
import logging
import os
from pathlib import Path
import re
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .auth import AdminError, AuthStore
from .clients import InternalClient


def create_app(env=None, management=None, maintenance=None):
    env = dict(os.environ if env is None else env)
    auth = AuthStore(env)
    management = management or InternalClient(env.get("ADMIN_API_URL", "http://ai-service:8000"), env.get("SERVICE_MANAGEMENT_TOKEN", ""))
    maintenance = maintenance or InternalClient("http://localhost", env.get("ADMIN_MAINTENANCE_TOKEN", ""),
        httpx.AsyncHTTPTransport(uds=env.get("ADMIN_MAINTENANCE_SOCKET", "/run/platform-maintenance/worker.sock")))
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    templates = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=select_autoescape())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    cookie = "platform_admin_session"
    secure = env.get("ADMIN_COOKIE_SECURE", "true") == "true"

    def set_session(response, token):
        response.set_cookie(cookie, token, max_age=auth.ttl, path="/admin", httponly=True, secure=secure, samesite="strict")
        return response

    def session(request, *, login=False):
        token = request.cookies.get(cookie, "")
        record = auth.session(token)
        if not record or (not login and not record[1]):
            raise AdminError("authentication_required", 401)
        return token, record[0]

    async def body(request):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 65536:
                raise AdminError("request_too_large", 413)
        try:
            if request.headers.get("content-type", "").split(";")[0] == "application/json":
                result = json.loads(data)
                if not isinstance(result, dict):
                    raise ValueError
                return result
            fields = parse_qs(data.decode(), keep_blank_values=True, max_num_fields=32, strict_parsing=True)
            if any(len(values) != 1 for values in fields.values()):
                raise ValueError
            return {key: values[0] for key, values in fields.items()}
        except (ValueError, UnicodeError):
            raise AdminError("invalid_request", 422) from None

    async def mutation(request, *, login=False):
        token, csrf = session(request, login=login)
        data = await body(request)
        supplied = request.headers.get("x-csrf-token", data.pop("csrf", ""))
        if not isinstance(supplied, str) or not hmac.compare_digest(csrf, supplied):
            raise AdminError("csrf_rejected", 403)
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise AdminError("origin_rejected", 403)
        return token, data

    def page(name, csrf, **context):
        # The template context contains only explicit public fields, never env.
        return HTMLResponse(templates.get_template(name).render(csrf=csrf, **context))

    @app.middleware("http")
    async def security(request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            response = JSONResponse({"error": "admin_unavailable"}, status_code=503)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "Content-Security-Policy": "default-src 'none'; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"})
        return response

    @app.exception_handler(AdminError)
    async def failure(request, error):
        if error.code == "authentication_required" and not request.url.path.startswith("/admin/api/"):
            return RedirectResponse("/admin/login", status_code=303)
        if request.url.path.startswith("/admin/api/"):
            return JSONResponse({"error": error.code}, status_code=error.status)
        return HTMLResponse(templates.get_template("error.html").render(code=error.code), status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def invalid(_request, _error):
        return JSONResponse({"error": "invalid_request"}, status_code=422)

    @app.get("/health")
    def health():
        with auth.db():
            pass
        return {"status": "ok"}

    @app.get("/admin/login")
    def login_page(request: Request):
        token, csrf = auth.create_session(old=request.cookies.get(cookie, ""))
        return set_session(page("login.html", csrf), token)

    @app.post("/admin/login")
    async def login(request: Request):
        old, data = await mutation(request, login=True)
        if set(data) != {"username", "password"} or not all(isinstance(v, str) for v in data.values()):
            raise AdminError("invalid_request", 422)
        auth.login(data["username"], data["password"])
        token, _ = auth.create_session(True, old)
        return set_session(RedirectResponse("/admin", status_code=303), token)

    @app.post("/admin/logout")
    async def logout(request: Request):
        token, _ = await mutation(request)
        auth.logout(token)
        response = RedirectResponse("/admin/login", status_code=303)
        response.delete_cookie(cookie, path="/admin", secure=secure, httponly=True, samesite="strict")
        return response

    @app.get("/admin")
    @app.get("/admin/")
    async def home(request: Request):
        _, csrf = session(request)
        return page("home.html", csrf, username=auth.username)

    @app.get("/admin/password")
    def password_page(request: Request):
        _, csrf = session(request)
        return page("password.html", csrf)

    @app.post("/admin/password")
    async def password_change(request: Request):
        await mutation_password(request)
        return RedirectResponse("/admin/login", status_code=303)

    async def mutation_password(request):
        _, data = await mutation(request)
        if set(data) != {"current_password", "new_password"} or not all(isinstance(v, str) for v in data.values()):
            raise AdminError("invalid_request", 422)
        auth.change_password(data["current_password"], data["new_password"])

    @app.post("/admin/api/password")
    async def api_password(request: Request):
        await mutation_password(request)
        return {"status": "password_changed_login_required"}

    def resource_path(kind, key=""):
        if kind not in {"services", "domains", "ai", "prompts", "knowledge", "channels", "runtime"}:
            raise AdminError("not_found", 404)
        if key and (not re.fullmatch(r"[a-z0-9][a-z0-9._/-]{0,252}", key)
                    or any(part in {".", "..", ""} for part in key.split("/"))):
            raise AdminError("invalid_identifier", 422)
        return "/management/" + (kind if kind in {"services", "domains"} else "profiles/" + kind) + ("/" + key if key else "")

    async def public_call(kind, method, key="", data=None):
        # Upstream schemas are strict; additionally reject known secret values
        # from user content, so a pasted internal token cannot enter a page.
        protected = (management.token, maintenance.token, auth.secret, *auth.protected_values())
        def sensitive(value):
            encoded = json.dumps(value, ensure_ascii=False)
            return any(secret and json.dumps(secret, ensure_ascii=False)[1:-1] in encoded for secret in protected)
        if data is not None:
            if sensitive(data):
                raise AdminError("sensitive_input_rejected", 422)
        result = await management.call(method, resource_path(kind, key), data)
        if sensitive(result):
            raise AdminError("unsafe_upstream_response", 502)
        return result

    @app.get("/admin/api/{kind}")
    async def api_list(kind: str, request: Request):
        session(request)
        return await public_call(kind, "GET")

    @app.get("/admin/api/{kind}/{key:path}")
    async def api_get(kind: str, key: str, request: Request):
        session(request)
        return await public_call(kind, "GET", key)

    @app.api_route("/admin/api/{kind}", methods=["POST"], status_code=201)
    async def api_create(kind: str, request: Request):
        _, data = await mutation(request)
        return await public_call(kind, "POST", data=data)

    @app.api_route("/admin/api/{kind}/{key:path}", methods=["PUT", "PATCH", "DELETE"])
    async def api_edit(kind: str, key: str, request: Request):
        _, data = await mutation(request)
        return await public_call(kind, request.method, key, data)

    @app.get("/admin/manage/{kind}")
    async def list_page(kind: str, request: Request):
        _, csrf = session(request)
        records = await public_call(kind, "GET")
        return page("records.html", csrf, kind=kind, records=records)

    @app.post("/admin/toggle/{kind}/{key}")
    async def toggle_page(kind: str, key: str, request: Request):
        _, data = await mutation(request)
        if kind not in {"services", "domains"} or set(data) != {"enabled"} or data["enabled"] not in {"true", "false"}:
            raise AdminError("invalid_request", 422)
        patch = {"enabled": data["enabled"] == "true"}
        if kind == "domains" and not patch["enabled"]:
            patch["default_entry"] = False
        await public_call(kind, "PATCH", key, patch)
        return RedirectResponse("/admin/manage/" + kind, status_code=303)

    @app.get("/admin/edit/{kind}/{key:path}")
    async def edit_page(kind: str, key: str, request: Request):
        _, csrf = session(request)
        resource_path(kind, key if key != "_new" else "")
        record = {} if key == "_new" else await public_call(kind, "GET", key)
        return page("edit.html", csrf, kind=kind, key=key, record=json.dumps(record, ensure_ascii=False, indent=2))

    @app.post("/admin/edit/{kind}/{key:path}")
    async def save_page(kind: str, key: str, request: Request):
        _, data = await mutation(request)
        if set(data) != {"record"} or not isinstance(data["record"], str):
            raise AdminError("invalid_request", 422)
        try:
            record = json.loads(data["record"])
        except ValueError:
            raise AdminError("invalid_json", 422) from None
        if not isinstance(record, dict):
            raise AdminError("invalid_request", 422)
        if key != "_new":
            record.pop("id" if kind != "domains" else "domain", None)
        await public_call(kind, "POST" if key == "_new" else "PUT", "" if key == "_new" else key, record)
        return RedirectResponse("/admin/manage/" + kind, status_code=303)

    @app.get("/admin/maintenance")
    async def maintenance_page(request: Request):
        _, csrf = session(request)
        state = await maintenance.call("GET", "/state")
        return page("maintenance.html", csrf, state=state)

    # Specific maintenance routes must precede the generic browser API routes;
    # use a distinct path to keep it outside resource dispatch.
    @app.get("/admin/system/state")
    async def maintenance_state(request: Request):
        session(request)
        return await maintenance.call("GET", "/state")

    @app.post("/admin/system/run")
    async def maintenance_run(request: Request):
        _, data = await mutation(request)
        result = await maintenance.call("POST", "/jobs", data)
        if request.headers.get("content-type", "").startswith("application/json"):
            return JSONResponse(result, status_code=202)
        return RedirectResponse("/admin/maintenance", status_code=303)

    app.state.auth = auth
    return app
