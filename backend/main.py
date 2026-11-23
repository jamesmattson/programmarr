#!/usr/bin/env python3
"""Programmarr web server — FastAPI backend."""

import asyncio
import base64
import hmac
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

# asyncio.create_subprocess_exec requires ProactorEventLoop on Windows
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

import scheduler
from routers import channels_router, config_router, logs_router, pipeline_router, recipes_router, status_router

DATA_DIR = Path(os.environ.get("PROGRAMMARR_DATA", Path(__file__).parent.parent))
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure data directories exist before serving any requests
    (DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
    # Start the live-channel scheduler loop (no-op unless recipes_enabled in config)
    scheduler_task = asyncio.create_task(scheduler.scheduler_loop())
    try:
        yield
    finally:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Programmarr", docs_url="/api/docs", redoc_url=None, lifespan=lifespan)

# SECURITY: no CORS middleware. The SPA is served from this same origin (and the
# Vite dev server proxies /api), so no other website ever needs to read this API.
# A wildcard CORS policy with credentials let any web page drive it.

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def is_cross_site(headers) -> bool:
    """True if a browser sent this request from another website.

    Browsers stamp every request with Sec-Fetch-Site; older ones at least send
    Origin on a cross-origin POST. A request with neither came from curl or a
    script, not a web page, so it is not CSRF and is allowed. Checking the
    browser's own label (not Host) keeps reverse proxies that rewrite Host working.
    """
    site = headers.get("sec-fetch-site")
    if site:
        return site not in ("same-origin", "none")
    origin = headers.get("origin")
    if origin is None:
        return False
    return urlsplit(origin).netloc != headers.get("host", "")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # CSRF guard: a state-changing request from another website is refused before
    # anything else, so a malicious page can't deploy, wipe, or rewrite config.
    if request.method not in _SAFE_METHODS and is_cross_site(request.headers):
        return Response(content="Cross-site request refused", status_code=403)

    config_path = DATA_DIR / "config.json"
    try:
        with open(config_path) as f:
            config = json.load(f)
    except FileNotFoundError:
        config = {}  # first run: no config yet, so no auth configured
    except Exception:
        # Fail closed: an unreadable config must not silently switch auth off.
        return Response(content="config.json could not be read", status_code=503)

    auth_user = config.get("auth_username", "")
    auth_pass = config.get("auth_password", "")

    if auth_user and auth_pass:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Basic "):
            return Response(
                content="Unauthorized",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Programmarr"'},
            )
        try:
            decoded = base64.b64decode(auth_header[6:]).decode()
            username, password = decoded.split(":", 1)
            if not (hmac.compare_digest(username.encode(), auth_user.encode())
                    & hmac.compare_digest(password.encode(), auth_pass.encode())):
                raise ValueError("bad credentials")
        except Exception:
            return Response(
                content="Unauthorized",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Programmarr"'},
            )

    return await call_next(request)


app.include_router(config_router.router, prefix="/api")
app.include_router(status_router.router, prefix="/api")
app.include_router(channels_router.router, prefix="/api")
app.include_router(pipeline_router.router, prefix="/api")
app.include_router(recipes_router.router, prefix="/api")
app.include_router(logs_router.router, prefix="/api")

if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(STATIC_DIR / "assets")), name="assets")

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon():
        return Response(content=(STATIC_DIR / "favicon.svg").read_bytes(), media_type="image/svg+xml")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        return HTMLResponse(content=(STATIC_DIR / "index.html").read_text())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=7979, reload=False)
