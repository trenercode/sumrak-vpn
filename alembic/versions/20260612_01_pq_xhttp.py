"""Add post-quantum XHTTP server identity fields."""

from alembic import op
import sqlalchemy as sa

revision = "20260612_01"
down_revision = "20260611_04"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("vpn_servers")}
    additions = {
        "pq_enabled": sa.Column(
            "pq_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        "vless_encryption": sa.Column("vless_encryption", sa.Text()),
        "vless_decryption": sa.Column("vless_decryption", sa.Text()),
        "reality_mldsa65_seed": sa.Column("reality_mldsa65_seed", sa.Text()),
        "reality_mldsa65_verify": sa.Column("reality_mldsa65_verify", sa.Text()),
        "reality_spider_x": sa.Column(
            "reality_spider_x", sa.String(255), nullable=False, server_default="/"
        ),
    }
    for name, column in additions.items():
        if name not in columns:
            op.add_column("vpn_servers", column)


def downgrade():
    for name in [
        "reality_spider_x",
        "reality_mldsa65_verify",
        "reality_mldsa65_seed",
        "vless_decryption",
        "vless_encryption",
        "pq_enabled",
    ]:
        op.drop_column("vpn_servers", name)
