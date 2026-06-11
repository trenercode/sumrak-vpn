"""Add remote Xray config management fields."""

from alembic import op
import sqlalchemy as sa

revision = "20260611_03"
down_revision = "20260611_02"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("vpn_servers")}
    additions = {
        "ssh_host": sa.Column("ssh_host", sa.String(255)),
        "ssh_port": sa.Column("ssh_port", sa.Integer(), nullable=False, server_default="22"),
        "ssh_user": sa.Column("ssh_user", sa.String(100)),
        "ssh_key_path": sa.Column("ssh_key_path", sa.Text()),
        "remote_xray_config_path": sa.Column("remote_xray_config_path", sa.Text()),
        "remote_compose_dir": sa.Column("remote_compose_dir", sa.Text()),
        "remote_container_name": sa.Column("remote_container_name", sa.String(255)),
    }
    for name, column in additions.items():
        if name not in columns:
            op.add_column("vpn_servers", column)


def downgrade():
    for name in [
        "remote_container_name",
        "remote_compose_dir",
        "remote_xray_config_path",
        "ssh_key_path",
        "ssh_user",
        "ssh_port",
        "ssh_host",
    ]:
        op.drop_column("vpn_servers", name)
