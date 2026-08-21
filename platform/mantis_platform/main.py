from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import Base, engine
from .routers import admin, evals, feedback, ingest, learning, traces

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(_app):
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="Mantis Platform", version=settings.schema_version, lifespan=lifespan,
              description="Agent learning and reliability platform: observe → evaluate → label → improve → validate → deploy.")

for r in (admin, ingest, traces, evals, feedback, learning):
    app.include_router(r.router)

# Idempotent; also covers in-process test clients that skip lifespan.
Base.metadata.create_all(engine)


@app.get("/healthz")
def healthz():
    return {"ok": True, "schema_version": settings.schema_version, "genai_semconv": settings.genai_semconv_version}


CONSOLE = Path(__file__).resolve().parents[2] / "console"
if CONSOLE.exists():
    app.mount("/console", StaticFiles(directory=str(CONSOLE), html=True), name="console")

    @app.get("/", include_in_schema=False)
    def root():
        return FileResponse(str(CONSOLE / "index.html"))
