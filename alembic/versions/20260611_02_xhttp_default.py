"""Make XHTTP the default transport for new VPN servers."""

from alembic import op

revision = "20260611_02"
down_revision = "20260611_01"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("vpn_servers") as batch:
        batch.alter_column("transport", server_default="xhttp")
        batch.alter_column("flow", server_default="")


def downgrade():
    with op.batch_alter_table("vpn_servers") as batch:
        batch.alter_column("flow", server_default="xtls-rprx-vision")
        batch.alter_column("transport", server_default="raw")
