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

    async def support_settings(csrf, *, error="", saved=False, status=200):
        state = await management.call("GET", "/management/support-entry")
        ensure_public(state)
        response = page("support.html", csrf, state=state, error=error, saved=saved)
        response.status_code = status
        return response

    @app.get("/admin/support")
    async def support_page(request: Request):
        _, csrf = session(request)
        return await support_settings(csrf, saved=request.query_params.get("saved") == "1")

    @app.post("/admin/support")
    async def support_save(request: Request):
        _, csrf = session(request)
        _, data = await mutation(request)
        if set(data) != {"service_id"} or not isinstance(data["service_id"], str):
            raise AdminError("invalid_request", 422)
        try:
            await management.call("PUT", "/management/support-entry", data)
        except AdminError:
            return await support_settings(csrf, error="保存失败，请确认实例已启用且已绑定有效的网站收件箱。", status=409)
        return RedirectResponse("/admin/support?saved=1", status_code=303)

    @app.get("/support/session.js")
    def support_script():
        return FileResponse(Path(__file__).parent / "static/support.js", media_type="text/javascript")

    @app.get("/support/")
    async def support(request: Request):
        try:
            entry = await management.call("GET", "/management/support-entry/public")
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
                        '<script src="/support/session.js" defer></script>')
            return HTMLResponse(document.replace("</head>", addition + "</head>"))
        except (AdminError, httpx.HTTPError, ValueError, KeyError, UnicodeError):
            return HTMLResponse('<!doctype html><html lang="zh-CN"><meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>在线客服</title><p>在线客服暂不可用，请稍后重试。</p></html>', status_code=503)

    return domains_page
