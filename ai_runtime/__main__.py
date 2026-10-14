import os

import uvicorn


if __name__ == "__main__":
    uvicorn.run(
        "ai_runtime.app:app", host="0.0.0.0",
        port=int(os.environ.get("AI_SERVICE_CONTAINER_PORT", "8000")),
        log_level="info", access_log=False,
    )
