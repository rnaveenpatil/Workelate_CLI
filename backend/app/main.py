"""FastAPI entrypoint: uvicorn app.main:app --reload --port 8000"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

if __package__:
    from .routes import router
else:
    # Allow `uvicorn main:app` from backend/app as well as the canonical
    # `uvicorn app.main:app` from backend/.
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.routes import router

app = FastAPI(title="Workelate_CLI", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frantend"
if FRONTEND_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=FRONTEND_DIR, html=True), name="ui")


@app.get("/")
def root() -> dict:
    return {
        "agent": "Workelate_CLI",
        "docs": "/docs",
        "health": "/health",
        "ui": "/ui/",
    }
