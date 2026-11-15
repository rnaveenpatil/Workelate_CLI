"""FastAPI entrypoint: uvicorn app.main:app --reload --port 8000"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routes import router

app = FastAPI(title="Workelate_CLI", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.get("/")
def root() -> dict:
    return {"agent": "Workelate_CLI", "docs": "/docs", "health": "/health"}
