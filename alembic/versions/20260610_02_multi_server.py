"""Add multi-server VPN architecture."""

from alembic import op
import sqlalchemy as sa

revision = "20260610_02"
down_revision = "20260610_01"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    device_columns = {column["name"] for column in inspector.get_columns("devices")}
    if "subscription_token" not in device_columns:
        op.add_column("devices", sa.Column("subscription_token", sa.String(64), nullable=True))
        if op.get_bind().dialect.name == "postgresql":
            op.execute("UPDATE devices SET subscription_token = md5(id || credential)")
        else:
            op.execute("UPDATE devices SET subscription_token = id || credential")
        op.alter_column("devices", "subscription_token", nullable=False)
        op.create_unique_constraint(
            "uq_devices_subscription_token", "devices", ["subscription_token"]
        )
        op.create_index("ix_devices_subscription_token", "devices", ["subscription_token"])

    if "vpn_servers" not in inspector.get_table_names():
        op.create_table(
        "vpn_servers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("country_code", sa.String(8), nullable=False),
        sa.Column("country_name", sa.String(100), nullable=False),
        sa.Column("city", sa.String(100), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("public_host", sa.String(255), nullable=False),
        sa.Column("public_port", sa.Integer(), nullable=False),
        sa.Column("protocol", sa.String(32), nullable=False),
        sa.Column("transport", sa.String(32), nullable=False),
        sa.Column("reality_server_name", sa.String(255), nullable=False),
        sa.Column("reality_public_key", sa.String(255), nullable=False),
        sa.Column("reality_short_id", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(32), nullable=False),
        sa.Column("flow", sa.String(64), nullable=False),
        sa.Column("xray_config_path", sa.Text()),
        sa.Column("management_mode", sa.String(32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("max_devices", sa.Integer()),
        sa.Column("current_devices", sa.Integer()),
        sa.Column("health_status", sa.String(20), nullable=False),
        sa.Column("last_health_check_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
        op.create_index("ix_vpn_servers_is_active", "vpn_servers", ["is_active"])
    if "device_server_profiles" not in inspector.get_table_names():
        op.create_table(
        "device_server_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("device_id", sa.String(36), sa.ForeignKey("devices.id"), nullable=False),
        sa.Column("server_id", sa.String(36), sa.ForeignKey("vpn_servers.id"), nullable=False),
        sa.Column("credential", sa.String(64), nullable=False, unique=True),
        sa.Column("client_email", sa.String(96), nullable=False, unique=True),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("last_activity_at", sa.DateTime(timezone=True)),
        sa.Column("transfer_rx", sa.BigInteger(), nullable=False),
        sa.Column("transfer_tx", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("device_id", "server_id", name="uq_device_server_profile"),
    )
        op.create_index("ix_device_server_profiles_device_id", "device_server_profiles", ["device_id"])
        op.create_index("ix_device_server_profiles_server_id", "device_server_profiles", ["server_id"])


def downgrade():
    op.drop_table("device_server_profiles")
    op.drop_table("vpn_servers")
    op.drop_index("ix_devices_subscription_token", table_name="devices")
    op.drop_constraint("uq_devices_subscription_token", "devices", type_="unique")
    op.drop_column("devices", "subscription_token")
