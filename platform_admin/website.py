"""Website forms and native top-level Chatwoot Widget delivery."""
import re
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
import httpx

from .auth import AdminError
from .site_domains import hostname, SiteError


def routes(app, *, env, session, mutation, page, management, maintenance, ensure_public, widget_transport=None):
    async def domains_page(csrf, *, notice="", error="", status=200):
        try:
            state = await maintenance.call("GET", "/website-domains")
            jobs = (await maintenance.call("GET", "/state"))["jobs"]
        except AdminError:
            state, jobs = {"domains": []}, []
            error = error or "域名维护服务不可用，请检查维护执行器状态。"
        ensure_public(state)
        response = page("websites.html", csrf, state=state, jobs=[r for r in jobs if r["operation"].startswith("domain-")],
                        error=error, notice=notice)
        response.status_code = status
        return response

    @app.get("/admin/websites")
    async def websites(request: Request):
        _, csrf = session(request)
        return await domains_page(csrf, notice="已提交维护操作，请刷新查看结果。操作成功后域名才会正式启用。" if request.query_params.get("submitted") else "")

    @app.post("/admin/websites")
    async def change(request: Request):
        _, csrf = session(request)
        _, data = await mutation(request)
        try:
            if (set(data) != {"action", "domain"} or not all(isinstance(v, str) for v in data.values())
                    or data["action"] not in {"add", "primary", "delete"}):
                raise SiteError("invalid_domain")
            domain = hostname(data["domain"].strip().lower())
            operation = "domain-" + data["action"]
            await maintenance.call("POST", "/jobs", {"operation": operation, "version": domain, "confirmation": operation.upper()})
        except (SiteError, AdminError):
            return await domains_page(csrf, error="操作未提交：请检查域名格式，或等待当前维护任务结束。", status=409)
        return RedirectResponse("/admin/websites?submitted=1", status_code=303)

    async def support_settings(csrf, *, error="", saved=False, status=200, edit="", draft=None):
        state = await management.call("GET", "/management/support-entry")
        entries = await management.call("GET", "/management/support-entries")
        options = await management.call("GET", "/management/support-entries/options")
        ensure_public(state)
        ensure_public(entries)
        ensure_public(options)
        record = next((r for r in entries if r["slug"] == edit), None) if edit else None
        if edit and record is None:
            raise AdminError("support_entry_not_found", 404)
        response = page("support.html", csrf, state=state, entries=entries, options=options,
                        edit=edit, record=draft or record or {"enabled": True}, error=error, saved=saved)
        response.status_code = status
        return response

    @app.get("/admin/support")
    async def support_page(request: Request):
        _, csrf = session(request)
        edit = request.query_params.get("edit", "")
        if edit and not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", edit):
            raise AdminError("invalid_request", 422)
        return await support_settings(csrf, saved=request.query_params.get("saved") == "1", edit=edit)

    @app.post("/admin/support")
    async def support_save(request: Request):
        _, csrf = session(request)
        _, data = await mutation(request)
        if any(not isinstance(value, str) for value in data.values()):
            raise AdminError("invalid_request", 422)
        action = data.pop("action", "legacy-default")
        old_slug = data.pop("original_slug", "")
        edit = old_slug if action == "save" else ""
        messages = {"support_slug_in_use": "此 Slug 已存在，请使用不同的链接名称。",
            "support_inbox_in_use": "此客服实例已用于其他入口。独立平台请使用不同的实例和收件箱。",
            "support_default_protected": "请先将另一个已启用入口设为默认，再停用或删除当前默认入口。",
            "invalid_support_slug": "Slug 必须以小写字母开头，仅包含小写字母、数字和单个连字符，最长 64 位。"}
        try:
            if action == "legacy-default" and set(data) == {"service_id"}:
                await management.call("PUT", "/management/support-entry", data)
            elif action == "save" and set(data) <= {"slug", "name", "service_id", "enabled"} and {"slug", "name", "service_id"} <= set(data):
                options = await management.call("GET", "/management/support-entries/options")
                option = next((o for o in options if o["service_id"] == data["service_id"]), None)
                if not option or data.get("enabled", "true") not in {"true", "false"}:
                    raise AdminError("invalid_support_inbox", 422)
                record = {"slug": data["slug"], "name": data["name"], "service_id": data["service_id"],
                          "enabled": data.get("enabled") == "true", "account_id": option["account_id"], "inbox_id": option["inbox_id"]}
                if old_slug:
                    if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", old_slug):
                        raise AdminError("invalid_request", 422)
                    await management.call("PUT", "/management/support-entries/" + old_slug, record)
                else:
                    await management.call("POST", "/management/support-entries", record)
            elif action in {"default", "delete", "toggle"} and set(data) == {"slug"} and re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", data["slug"]):
                endpoint = "/management/support-entries/" + data["slug"]
                if action == "default":
                    await management.call("POST", endpoint + "/default")
                elif action == "delete":
                    await management.call("DELETE", endpoint)
                else:
                    records = await management.call("GET", "/management/support-entries")
                    row = next((r for r in records if r["slug"] == data["slug"]), None)
                    if row is None:
                        raise AdminError("support_entry_not_found", 404)
                    await management.call("PUT", endpoint, {k: (not row[k] if k == "enabled" else row[k])
                        for k in ("name", "service_id", "account_id", "inbox_id", "enabled")})
            else:
                raise AdminError("invalid_request", 422)
        except AdminError as exc:
            return await support_settings(csrf, error=messages.get(exc.code, "操作失败，请检查客服实例、Chatwoot 网站收件箱绑定及连接状态。"),
                status=409, edit=edit, draft=data if action == "save" else None)
        return RedirectResponse("/admin/support?saved=1", status_code=303)

    @app.get("/support/session.js")
    def support_script():
        return FileResponse(Path(__file__).parent / "static/support.js", media_type="text/javascript")

    @app.get("/admin/assets/support-entries.js")
    def entry_script(request: Request):
        session(request)
        return FileResponse(Path(__file__).parent / "static/support-entries.js", media_type="text/javascript")

    @app.get("/support/")
    @app.get("/support/{slug}/")
    async def support(request: Request, slug: str | None = None):
        try:
            if slug is not None and (len(slug) > 64 or not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", slug)):
                return support_error("此客服入口不存在。", 404)
            try:
                entry = await management.call("GET", "/management/support-entry/public" + ("/" + slug if slug is not None else ""))
            except AdminError as exc:
                if exc.code == "support_entry_not_found":
                    return support_error("此客服入口不存在。", 404)
                if exc.code == "support_entry_disabled":
                    return support_error("此客服入口已停用。", 410)
                raise
            token = entry["website_token"]
            if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,256}", token):
                raise ValueError
            params = {"website_token": token}
            previous = request.cookies.get("platform_support_" + token, "")
            if re.fullmatch(r"[A-Za-z0-9_.-]{1,4096}", previous):
                params["cw_conversation"] = previous
            async with httpx.AsyncClient(base_url=env.get("ADMIN_CHATWOOT_URL", "http://rails:3000"),
                    transport=widget_transport, trust_env=False, timeout=15, follow_redirects=False) as client:
                async with client.stream("GET", "/widget", params=params, headers={
                        "Host": request.headers["host"], "X-Forwarded-Proto": request.url.scheme,
                        "X-Forwarded-For": request.client.host if request.client else ""}) as upstream:
                    if upstream.status_code != 200:
                        raise ValueError
                    raw = bytearray()
                    async for chunk in upstream.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 2_097_152:
                            raise ValueError
            document = raw.decode("utf-8")
            if "window.chatwootWebChannel" not in document or "window.authToken" not in document or "</head>" not in document:
                raise ValueError
            # Native top-level mode: no SDK bubble/iframe/DOM hiding hacks.
            addition = ('<style>html,body{margin:0;height:100%;overflow:hidden}#app{height:var(--support-height,100dvh)}</style>'
                        '<script src="/support/session.js"></script>')
            return HTMLResponse(document.replace("</head>", addition + "</head>"))
        except (AdminError, httpx.HTTPError, ValueError, KeyError, UnicodeError):
            return support_error("在线客服暂不可用，请稍后重试。", 503)

    def support_error(message, status):
        return HTMLResponse('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>在线客服</title><body><p role="alert">' + message + '</p></body></html>', status_code=status)

    return domains_page
