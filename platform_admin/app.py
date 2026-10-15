"""Single session-authenticated UI and internal browser API, under /admin."""

import hmac
import json
import logging
import os
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .auth import AdminError, AuthStore
from .clients import InternalClient
from . import forms


def origin_tuple(value):
    """A single HTTP(S) origin, not a base URL, path or forwarding header.

    Default ports and DNS case do not change an origin. Paths (including an
    ASGI root_path), credentials, opaque origins and ambiguous headers do.
    """
    if not value or re.search(r"[\s\\,?#]", value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.netloc.endswith(":")):
            return None
        port = parsed.port if parsed.port is not None else {"http": 80, "https": 443}[parsed.scheme]
        if not 1 <= port <= 65535:
            return None
        return parsed.scheme, parsed.hostname.lower(), port
    except ValueError:
        return None


def create_app(env=None, management=None, maintenance=None):
    env = dict(os.environ if env is None else env)
    auth = AuthStore(env)
    management = management or InternalClient(env.get("ADMIN_API_URL", "http://ai-service:8000"), env.get("SERVICE_MANAGEMENT_TOKEN", ""))
    maintenance = maintenance or InternalClient("http://localhost", env.get("ADMIN_MAINTENANCE_TOKEN", ""),
        httpx.AsyncHTTPTransport(uds=env.get("ADMIN_MAINTENANCE_SOCKET", "/run/platform-maintenance/worker.sock")))
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    templates = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=select_autoescape())
    templates.globals.update(titles=forms.TITLES, error_message=forms.error_message)
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

    async def body(request, *, max_fields=32, max_bytes=65536):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > max_bytes:
                raise AdminError("request_too_large", 413)
        try:
            if request.headers.get("content-type", "").split(";")[0] == "application/json":
                result = json.loads(data)
                if not isinstance(result, dict):
                    raise ValueError
                return result
            fields = parse_qs(data.decode(), keep_blank_values=True, max_num_fields=max_fields,
                              strict_parsing=True, errors="strict")
            if any(len(values) != 1 for values in fields.values()):
                raise ValueError
            return {key: values[0] for key, values in fields.items()}
        except (ValueError, UnicodeError):
            raise AdminError("invalid_request", 422) from None

    async def mutation(request, *, login=False, max_fields=32, max_bytes=65536):
        token, csrf = session(request, login=login)
        data = await body(request, max_fields=max_fields, max_bytes=max_bytes)
        supplied = request.headers.get("x-csrf-token", data.pop("csrf", ""))
        if not isinstance(supplied, str) or not hmac.compare_digest(csrf, supplied):
            raise AdminError("csrf_rejected", 403)
        origins = request.headers.getlist("origin")
        if origins:
            # Uvicorn applies the trusted proxy's protocol. Nginx must retain
            # the external Host authority (including its port). Do not trust
            # browser-supplied Forwarded/X-Forwarded-Host/Port here.
            origin = origin_tuple(origins[0])
            hosts = request.headers.getlist("host")
            if (len(origins) != 1 or len(hosts) != 1 or origin is None
                    or origin != origin_tuple(request.url.scheme + "://" + hosts[0])):
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
        # Fetch uses this document policy when generating a native form POST's
        # Origin. no-referrer would make even a same-origin form send "null".
        # Keep the origin for our forms, but send no Referer across origins.
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "same-origin", "Content-Security-Policy": "default-src 'none'; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"})
        if request.url.path in {"/admin/manage/knowledge", "/admin/knowledge"}:
            response.headers["Content-Security-Policy"] += "; script-src 'self'"
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

    @app.get("/ready")
    async def ready():
        # Never return dependency bodies, credentials or error details.
        try:
            with auth.db():
                pass
            services = await management.call("GET", "/management/services")
            domains = await management.call("GET", "/management/domains")
            models = await management.call("GET", "/management/profiles/ai")
            if not all(isinstance(items, list) for items in (services, domains, models)):
                raise AdminError("dependency_not_ready", 503)
            if not await management.authentication_enforced("/management/services"):
                raise AdminError("dependency_not_ready", 503)
            state = await maintenance.call("GET", "/state")
            if not isinstance(state, dict) or not isinstance(state.get("jobs"), list):
                raise AdminError("dependency_not_ready", 503)
        except Exception:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return {"status": "ready"}

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

    def ensure_public(value):
        # Upstream schemas are strict; additionally reject known secret values
        # from user content, so a pasted internal token cannot enter a page.
        protected = (management.token, maintenance.token, auth.secret, *auth.protected_values())
        encoded = json.dumps(value, ensure_ascii=False)
        if any(secret and json.dumps(secret, ensure_ascii=False)[1:-1] in encoded for secret in protected):
            raise AdminError("sensitive_input_rejected", 422)

    async def public_call(kind, method, key="", data=None):
        if data is not None:
            ensure_public(data)
        result = await management.call(method, resource_path(kind, key), data)
        try:
            ensure_public(result)
        except AdminError:
            raise AdminError("unsafe_upstream_response", 502) from None
        return result

    async def knowledge_call(service_id, method="GET", data=None):
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", service_id):
            raise AdminError("invalid_identifier", 422)
        if data is not None:
            ensure_public(data)
        result = await management.call(method, "/management/services/" + service_id + "/knowledge", data)
        ensure_public(result)
        return result

    @app.get("/admin/static/knowledge.js")
    def knowledge_script(request: Request):
        session(request)
        return FileResponse(Path(__file__).parent / "static" / "knowledge.js", media_type="text/javascript")

    async def knowledge_page(csrf, selected="", *, draft=None, error="", saved=False, status=200):
        services = await public_call("services", "GET")
        document = None
        if selected and draft is None:
            try:
                document = await knowledge_call(selected)
            except AdminError as exc:
                error, status = forms.error_message(exc.code), exc.status
        elif draft is not None:
            document = draft
        response = page("knowledge.html", csrf, kind="knowledge",
                        records=await public_call("knowledge", "GET"), services=services,
                        selected=selected, document=document, error=error, saved=saved, dirty=draft is not None)
        response.status_code = status
        return response

    @app.post("/admin/knowledge")
    async def save_knowledge(request: Request):
        _, csrf = session(request)
        _, data = await mutation(request, max_fields=6, max_bytes=10_000_000)
        ensure_public(data)
        if (set(data) != {"service_id", "content", "revision", "action"}
                or not all(isinstance(value, str) for value in data.values())
                or data["action"] not in {"save", "clear"}):
            raise AdminError("invalid_request", 422)
        selected = data["service_id"]
        try:
            payload = {"revision": data["revision"]}
            if data["action"] == "save":
                payload["content"] = data["content"]
            await knowledge_call(selected, "PUT" if data["action"] == "save" else "DELETE", payload)
        except AdminError as exc:
            if exc.code in {"sensitive_input_rejected", "unsafe_upstream_response", "invalid_identifier"}:
                raise
            return await knowledge_page(csrf, selected, draft={"content": data["content"], "revision": data["revision"]},
                                        error=forms.error_message(exc.code), status=exc.status)
        return RedirectResponse("/admin/manage/knowledge?service_id=" + selected + "&saved=1", status_code=303)

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
        if kind == "knowledge":
            return await knowledge_page(csrf, request.query_params.get("service_id", ""),
                                        saved=request.query_params.get("saved") == "1")
        records = await public_call(kind, "GET")
        return page("records.html", csrf, kind=kind, records=records, saved=request.query_params.get("saved") == "1")

    @app.post("/admin/toggle/{kind}/{key}")
    async def toggle_page(kind: str, key: str, request: Request):
        _, data = await mutation(request)
        if kind not in {"services", "domains"} or set(data) != {"enabled"} or data["enabled"] not in {"true", "false"}:
            raise AdminError("invalid_request", 422)
        patch = {"enabled": data["enabled"] == "true"}
        if kind == "domains" and not patch["enabled"]:
            patch["default_entry"] = False
        await public_call(kind, "PATCH", key, patch)
        return RedirectResponse("/admin/manage/" + kind + "?saved=1", status_code=303)

    async def render_editor(kind, key, csrf, record, *, error="", invalid="", rows=None, status=200):
        record = {**forms.defaults(kind), **record}
        options = {}
        for field in forms.FIELDS[kind]:
            if field.control == "select":
                records = await public_call(field.choices, "GET")
                options[field.name] = forms.choices(field, records, record.get(field.name, ""))
        if rows is None:
            rows = record.get("reply_rules", [])
        response = page("edit.html", csrf, kind=kind, key=key, record=record,
                        identity=forms.identity(kind), fields=forms.FIELDS[kind], options=options,
                        rows=rows or [""], error=error, invalid=invalid)
        response.status_code = status
        return response

    @app.get("/admin/edit/{kind}/{key:path}")
    async def edit_page(kind: str, key: str, request: Request):
        _, csrf = session(request)
        resource_path(kind, key if key != "_new" else "")
        if kind == "runtime" and key == "_new":
            return RedirectResponse("/admin/edit/runtime/default", status_code=303)
        record = ({"source": "knowledge.txt"} if kind == "knowledge" else {}) if key == "_new" else await public_call(kind, "GET", key)
        return await render_editor(kind, key, csrf, record)

    @app.post("/admin/edit/{kind}/{key:path}")
    async def save_page(kind: str, key: str, request: Request):
        # Only HTML editors need room for 64 independently editable rules and
        # URL-encoded Unicode. JSON API, login and other request limits stay as-is.
        _, csrf = session(request)
        _, data = await mutation(request, max_fields=72, max_bytes=4_194_304)
        resource_path(kind, key if key != "_new" else "")
        ensure_public(data)  # Never echo credentials even on a local form error.
        if kind == "runtime" and key == "_new":
            raise AdminError("invalid_request", 422)
        creating = key == "_new"
        existing = {} if creating else await public_call(kind, "GET", key)
        # Backward compatibility for pre-form HTML clients; no JSON editor is
        # rendered, and the existing /admin/api/* contract is unchanged.
        if set(data) == {"record"}:
            if not isinstance(data["record"], str):
                raise AdminError("invalid_request", 422)
            try:
                record = json.loads(data["record"])
            except ValueError:
                raise AdminError("invalid_json", 422) from None
            if not isinstance(record, dict):
                raise AdminError("invalid_request", 422)
            record = {**existing, **record}
        else:
            draft, rows = forms.draft(kind, data, existing, creating=creating)
            try:
                action = data.get("_action", "save")
                if action != "save" and (not isinstance(action, str) or kind != "prompts" or not re.fullmatch(r"add_rule|remove_rule_(?:[0-9]|[1-5][0-9]|6[0-3])", action)):
                    raise forms.FormError("不支持的表单操作。")
                record = forms.decode(kind, data, existing, creating=creating, validate=action == "save")
                if action != "save":
                    rows = rows or [""]
                    if action == "add_rule":
                        if len(rows) >= 64:
                            raise forms.FormError("回复规则最多 64 条。", "reply_rules")
                        rows.append("")
                    else:
                        index = int(action[12:])
                        if index >= len(rows):
                            raise forms.FormError("待删除的规则已不存在，请刷新后重试。", "reply_rules")
                        rows.pop(index)
                    return await render_editor(kind, key, csrf, draft, rows=rows)
            except forms.FormError as error:
                return await render_editor(kind, key, csrf, draft, rows=rows, error=error.message, invalid=error.field, status=422)
        if not creating:
            record.pop("id" if kind != "domains" else "domain", None)
        try:
            await public_call(kind, "POST" if creating else "PUT", "" if creating else key, record)
        except AdminError as error:
            if error.code in {"sensitive_input_rejected", "unsafe_upstream_response"}:
                raise
            return await render_editor(kind, key, csrf, record, error=forms.error_message(error.code), status=error.status)
        return RedirectResponse("/admin/manage/" + kind + "?saved=1", status_code=303)

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
