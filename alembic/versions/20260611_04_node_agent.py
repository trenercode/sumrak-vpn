"""Add node enrollment and agent status fields."""

from alembic import op
import sqlalchemy as sa

revision = "20260611_04"
down_revision = "20260611_03"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("vpn_servers")}
    additions = {
        "agent_token": sa.Column("agent_token", sa.String(128)),
        "agent_last_seen_at": sa.Column("agent_last_seen_at", sa.DateTime(timezone=True)),
        "agent_last_sync_at": sa.Column("agent_last_sync_at", sa.DateTime(timezone=True)),
        "agent_version": sa.Column("agent_version", sa.String(64)),
        "agent_last_error": sa.Column("agent_last_error", sa.Text()),
        "agent_clients_count": sa.Column("agent_clients_count", sa.Integer()),
    }
    for name, column in additions.items():
        if name not in columns:
            op.add_column("vpn_servers", column)
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("vpn_servers")}
    if "ix_vpn_servers_agent_token" not in indexes:
        op.create_index("ix_vpn_servers_agent_token", "vpn_servers", ["agent_token"], unique=True)
    if "node_enrollments" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "node_enrollments",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("node_token", sa.String(128), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True)),
            sa.Column("server_name", sa.String(100), nullable=False),
            sa.Column("expected_country_code", sa.String(8), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    indexes = {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes("node_enrollments")
    }
    if "ix_node_enrollments_node_token" not in indexes:
        op.create_index(
            "ix_node_enrollments_node_token", "node_enrollments", ["node_token"], unique=True
        )
    if "ix_node_enrollments_status" not in indexes:
        op.create_index("ix_node_enrollments_status", "node_enrollments", ["status"])


def downgrade():
    op.drop_table("node_enrollments")
    op.drop_index("ix_vpn_servers_agent_token", table_name="vpn_servers")
    for name in [
        "agent_clients_count",
        "agent_last_error",
        "agent_version",
        "agent_last_sync_at",
        "agent_last_seen_at",
        "agent_token",
    ]:
        op.drop_column("vpn_servers", name)
