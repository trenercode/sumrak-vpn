"""Add XHTTP transport settings."""

from alembic import op
import sqlalchemy as sa

revision = "20260611_01"
down_revision = "20260610_02"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("vpn_servers")}
    if "reality_target" not in columns:
        op.add_column(
            "vpn_servers",
            sa.Column(
                "reality_target",
                sa.String(255),
                nullable=False,
                server_default="www.microsoft.com:443",
            ),
        )
    if "xhttp_path" not in columns:
        op.add_column(
            "vpn_servers",
            sa.Column("xhttp_path", sa.String(255), nullable=False, server_default="/"),
        )
    if "xhttp_mode" not in columns:
        op.add_column(
            "vpn_servers",
            sa.Column("xhttp_mode", sa.String(32), nullable=False, server_default="auto"),
        )


def downgrade():
    op.drop_column("vpn_servers", "xhttp_mode")
    op.drop_column("vpn_servers", "xhttp_path")
    op.drop_column("vpn_servers", "reality_target")
