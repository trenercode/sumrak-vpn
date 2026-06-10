from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi import Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import router as admin_router
from app.config import get_settings
from app.db import engine, get_session
from app.models import Base, Device
from app.nodes import NodeManagerRegistry, ensure_default_server
from app.services import subscription_uris

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    from app.db import SessionLocal

    async with SessionLocal() as session:
        await ensure_default_server(session, get_settings())
    yield
    await engine.dispose()


app = FastAPI(title="VPN Service", lifespan=lifespan)
app.state.templates = Jinja2Templates(directory=BASE_DIR / "templates")
app.state.nodes = NodeManagerRegistry(get_settings())
app.include_router(admin_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/sub/{token}", response_class=PlainTextResponse)
async def subscription(
    token: str,
    base64_format: bool = Query(False, alias="base64"),
    session: AsyncSession = Depends(get_session),
):
    device = await session.scalar(select(Device).where(Device.subscription_token == token))
    if device is None:
        raise HTTPException(404, "Subscription not found")
    uris = await subscription_uris(session, device, get_settings(), app.state.nodes)
    content = "\n".join(uris)
    if base64_format:
        import base64

        content = base64.b64encode(content.encode()).decode()
    return PlainTextResponse(content)
