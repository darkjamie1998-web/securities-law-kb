# app/main.py — FastAPI 入口：健康检查 + 静态页面挂载
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="证券法律法规知识库")

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"


@app.get("/api/health")
def health():
    return {"status": "ok"}


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")
