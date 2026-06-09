import uuid
from datetime import UTC, datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str] = mapped_column(String(160), default="")
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    subscription_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    devices: Mapped[list["Device"]] = relationship(back_populates="user")


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    credential: Mapped[str] = mapped_column(String(64), unique=True)
    client_email: Mapped[str] = mapped_column(String(96), unique=True)
    is_revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    transfer_rx: Mapped[int] = mapped_column(BigInteger, default=0)
    transfer_tx: Mapped[int] = mapped_column(BigInteger, default=0)

    user: Mapped[User] = relationship(back_populates="devices")
