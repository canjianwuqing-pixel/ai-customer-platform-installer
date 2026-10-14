import os
import uvicorn

from .app import create_app

uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("AI_ADMIN_CONTAINER_PORT", "8080")),
            access_log=False, log_level="warning", proxy_headers=True, forwarded_allow_ips="*")
