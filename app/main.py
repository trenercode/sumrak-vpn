from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from app.admin import router as admin_router
from app.config import get_settings
from app.db import engine
from app.models import Base
from app.vpn import build_vpn_backend

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


app = FastAPI(title="VPN Service", lifespan=lifespan)
app.state.templates = Jinja2Templates(directory=BASE_DIR / "templates")
app.state.vpn = build_vpn_backend(get_settings())
app.include_router(admin_router)


@app.get("/health")
async def health():
    return {"status": "ok"}

