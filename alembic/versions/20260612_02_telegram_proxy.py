"""Add independent Telegram MTProto proxy nodes."""

from alembic import op
import sqlalchemy as sa

revision = "20260612_02"
down_revision = "20260612_01"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "telegram_proxy_nodes" not in tables:
        op.create_table(
            "telegram_proxy_nodes",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("country_code", sa.String(8), nullable=False, server_default=""),
            sa.Column("public_host", sa.String(255), nullable=False, server_default=""),
            sa.Column("public_port", sa.Integer(), nullable=False, server_default="443"),
            sa.Column("secret", sa.Text()),
            sa.Column("sponsor_tag", sa.Text()),
            sa.Column("sponsor_channel", sa.String(255)),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("health_error", sa.Text()),
            sa.Column("last_seen_at", sa.DateTime(timezone=True)),
            sa.Column("last_sync_at", sa.DateTime(timezone=True)),
            sa.Column("version", sa.String(64)),
            sa.Column("active_connections", sa.Integer()),
            sa.Column("traffic_bytes", sa.BigInteger()),
            sa.Column("current_config_hash", sa.String(64)),
            sa.Column("install_token_hash", sa.String(64), unique=True),
            sa.Column("install_token_expires_at", sa.DateTime(timezone=True)),
            sa.Column("agent_token_hash", sa.String(64), unique=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        for column in ["enabled", "status", "install_token_hash", "agent_token_hash"]:
            op.create_index(f"ix_telegram_proxy_nodes_{column}", "telegram_proxy_nodes", [column])
    if "telegram_proxy_events" not in tables:
        op.create_table(
            "telegram_proxy_events",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "node_id",
                sa.String(36),
                sa.ForeignKey("telegram_proxy_nodes.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("event_type", sa.String(32), nullable=False),
            sa.Column("message", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_telegram_proxy_events_node_id", "telegram_proxy_events", ["node_id"])
        op.create_index(
            "ix_telegram_proxy_events_event_type", "telegram_proxy_events", ["event_type"]
        )


def downgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "telegram_proxy_events" in tables:
        op.drop_table("telegram_proxy_events")
    if "telegram_proxy_nodes" in tables:
        op.drop_table("telegram_proxy_nodes")
