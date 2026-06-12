import secrets
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


def new_referral_code() -> str:
    return secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str] = mapped_column(String(160), default="")
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    subscription_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    referral_code: Mapped[str] = mapped_column(
        String(24), unique=True, index=True, default=new_referral_code
    )
    referred_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    referral_bonus_awarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_payment_discount_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    first_paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    devices: Mapped[list["Device"]] = relationship(back_populates="user")
    referrer: Mapped["User | None"] = relationship(remote_side=[id], foreign_keys=[referred_by_user_id])


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    platform: Mapped[str | None] = mapped_column(String(32))
    subscription_token: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, default=lambda: secrets.token_urlsafe(32)
    )
    credential: Mapped[str] = mapped_column(String(64), unique=True)
    client_email: Mapped[str] = mapped_column(String(96), unique=True)
    is_revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    transfer_rx: Mapped[int] = mapped_column(BigInteger, default=0)
    transfer_tx: Mapped[int] = mapped_column(BigInteger, default=0)

    user: Mapped[User] = relationship(back_populates="devices")
    server_profiles: Mapped[list["DeviceServerProfile"]] = relationship(
        back_populates="device", cascade="all, delete-orphan"
    )


class VpnServer(Base):
    __tablename__ = "vpn_servers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100))
    country_code: Mapped[str] = mapped_column(String(8), default="")
    country_name: Mapped[str] = mapped_column(String(100), default="")
    city: Mapped[str] = mapped_column(String(100), default="")
    host: Mapped[str] = mapped_column(String(255), default="")
    public_host: Mapped[str] = mapped_column(String(255))
    public_port: Mapped[int] = mapped_column(Integer, default=8443)
    protocol: Mapped[str] = mapped_column(String(32), default="vless-reality")
    transport: Mapped[str] = mapped_column(String(32), default="xhttp")
    reality_target: Mapped[str] = mapped_column(String(255), default="www.microsoft.com:443")
    reality_server_name: Mapped[str] = mapped_column(String(255))
    reality_public_key: Mapped[str] = mapped_column(String(255))
    reality_short_id: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(32), default="chrome")
    flow: Mapped[str] = mapped_column(String(64), default="")
    xhttp_path: Mapped[str] = mapped_column(String(255), default="/")
    xhttp_mode: Mapped[str] = mapped_column(String(32), default="auto")
    pq_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    vless_encryption: Mapped[str | None] = mapped_column(Text)
    vless_decryption: Mapped[str | None] = mapped_column(Text)
    reality_mldsa65_seed: Mapped[str | None] = mapped_column(Text)
    reality_mldsa65_verify: Mapped[str | None] = mapped_column(Text)
    reality_spider_x: Mapped[str] = mapped_column(String(255), default="/")
    xray_config_path: Mapped[str | None] = mapped_column(Text)
    management_mode: Mapped[str] = mapped_column(String(32), default="manual")
    ssh_host: Mapped[str | None] = mapped_column(String(255))
    ssh_port: Mapped[int] = mapped_column(Integer, default=22)
    ssh_user: Mapped[str | None] = mapped_column(String(100))
    ssh_key_path: Mapped[str | None] = mapped_column(Text)
    remote_xray_config_path: Mapped[str | None] = mapped_column(Text)
    remote_compose_dir: Mapped[str | None] = mapped_column(Text)
    remote_container_name: Mapped[str | None] = mapped_column(String(255))
    agent_token: Mapped[str | None] = mapped_column(String(128), unique=True, index=True)
    agent_last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    agent_last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    agent_version: Mapped[str | None] = mapped_column(String(64))
    agent_last_error: Mapped[str | None] = mapped_column(Text)
    agent_clients_count: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    max_devices: Mapped[int | None] = mapped_column(Integer)
    current_devices: Mapped[int | None] = mapped_column(Integer)
    health_status: Mapped[str] = mapped_column(String(20), default="unknown")
    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    profiles: Mapped[list["DeviceServerProfile"]] = relationship(back_populates="server")


class NodeEnrollment(Base):
    __tablename__ = "node_enrollments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    node_token: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    server_name: Mapped[str] = mapped_column(String(100))
    expected_country_code: Mapped[str] = mapped_column(String(8), default="")
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DeviceServerProfile(Base):
    __tablename__ = "device_server_profiles"
    __table_args__ = (UniqueConstraint("device_id", "server_id", name="uq_device_server_profile"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), index=True)
    server_id: Mapped[str] = mapped_column(ForeignKey("vpn_servers.id"), index=True)
    credential: Mapped[str] = mapped_column(String(64), unique=True)
    client_email: Mapped[str] = mapped_column(String(96), unique=True)
    uri: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    transfer_rx: Mapped[int] = mapped_column(BigInteger, default=0)
    transfer_tx: Mapped[int] = mapped_column(BigInteger, default=0)

    device: Mapped[Device] = relationship(back_populates="server_profiles")
    server: Mapped[VpnServer] = relationship(back_populates="profiles")


class ReferralReward(Base):
    __tablename__ = "referral_rewards"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    referrer_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    referred_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), unique=True)
    days_awarded: Mapped[int] = mapped_column(Integer, default=15)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NotificationLog(Base):
    __tablename__ = "notification_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    type: Mapped[str] = mapped_column(String(180), index=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    metadata_json: Mapped[dict | None] = mapped_column("metadata", JSON)


class VpnClient(Base):
    __tablename__ = "vpn_clients"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    download_url: Mapped[str] = mapped_column(Text)
    instruction_text: Mapped[str] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=100)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Broadcast(Base):
    __tablename__ = "broadcasts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    admin_id: Mapped[str | None] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(160))
    text: Mapped[str] = mapped_column(Text)
    image_file_id_or_url: Mapped[str | None] = mapped_column(Text)
    target_type: Mapped[str] = mapped_column(String(32))
    target_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    recipients: Mapped[list["BroadcastRecipient"]] = relationship(
        back_populates="broadcast", cascade="all, delete-orphan"
    )


class BroadcastRecipient(Base):
    __tablename__ = "broadcast_recipients"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    broadcast_id: Mapped[str] = mapped_column(ForeignKey("broadcasts.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    broadcast: Mapped[Broadcast] = relationship(back_populates="recipients")
