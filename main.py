import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from time import perf_counter

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from database import init_db, cleanup_expired_data
from routers import agents, tools, rag, execute, settings as settings_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("agent-orchestrator.api")


async def periodic_cleanup():
    while True:
        try:
            await asyncio.sleep(settings.cleanup_interval_seconds)
            cleanup_expired_data()
            logger.info("Expired data cleanup complete")
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Cleanup sweep failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("Starting Agent Orchestrator API | frontend=%s", settings.frontend_url)
    init_db()
    cleanup_task = asyncio.create_task(periodic_cleanup())
    logger.info("Startup complete | cleanup sweep every %ss", settings.cleanup_interval_seconds)
    yield
    logger.info("Shutting down Agent Orchestrator API")
    cleanup_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await cleanup_task


app = FastAPI(title="Agent Orchestrator API", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def log_request(request: Request, call_next):
    started = perf_counter()
    response = await call_next(request)
    logger.info(
        "%s %s -> %s | %.1f ms",
        request.method,
        request.url.path,
        response.status_code,
        (perf_counter() - started) * 1000,
    )
    return response


allowed_origins = settings.frontend_url_set
if "localhost" in settings.frontend_url:
    allowed_origins.add(settings.frontend_url.replace("localhost", "127.0.0.1"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(allowed_origins),
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(agents.router)
app.include_router(tools.router)
app.include_router(rag.router)
app.include_router(execute.router)
app.include_router(settings_router.router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "agent-orchestrator"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.port,
        reload=True,
        reload_excludes=["*.db", "*.db-wal", "*.db-shm", "*.log", "uploads/*"],
    )
