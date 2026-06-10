"""Add referrals, notifications, clients, broadcasts, and device platform."""

from alembic import op
import sqlalchemy as sa

revision = "20260610_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "users" not in inspector.get_table_names():
        from app.models import Base

        Base.metadata.create_all(bind=bind)
        return

    op.add_column("users", sa.Column("referral_code", sa.String(24), nullable=True))
    op.add_column("users", sa.Column("referred_by_user_id", sa.String(36), nullable=True))
    op.add_column("users", sa.Column("referral_bonus_awarded_at", sa.DateTime(timezone=True)))
    op.add_column("users", sa.Column("first_payment_discount_used_at", sa.DateTime(timezone=True)))
    op.add_column("users", sa.Column("first_paid_at", sa.DateTime(timezone=True)))
    op.execute("UPDATE users SET referral_code = substr(md5(id || telegram_id::text), 1, 12)")
    op.alter_column("users", "referral_code", nullable=False)
    op.create_unique_constraint("uq_users_referral_code", "users", ["referral_code"])
    op.create_index("ix_users_referred_by_user_id", "users", ["referred_by_user_id"])
    op.create_foreign_key(
        "fk_users_referred_by", "users", "users", ["referred_by_user_id"], ["id"]
    )
    op.add_column("devices", sa.Column("platform", sa.String(32), nullable=True))
    # Compatibility with databases left from the early WireGuard MVP.
    device_columns = {column["name"] for column in inspector.get_columns("devices")}
    if "public_key" in device_columns:
        op.alter_column("devices", "public_key", nullable=True)
    if "assigned_ip" in device_columns:
        op.alter_column("devices", "assigned_ip", nullable=True)

    op.create_table(
        "referral_rewards",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("referrer_user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("referred_user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("days_awarded", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("referred_user_id"),
    )
    op.create_table(
        "notification_log",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("type", sa.String(180), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata", sa.JSON()),
    )
    op.create_index("ix_notification_log_user_id", "notification_log", ["user_id"])
    op.create_index("ix_notification_log_type", "notification_log", ["type"])
    op.create_table(
        "vpn_clients",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("download_url", sa.Text(), nullable=False),
        sa.Column("instruction_text", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "broadcasts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("admin_id", sa.String(100)),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("image_file_id_or_url", sa.Text()),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_user_id", sa.String(36), sa.ForeignKey("users.id")),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "broadcast_recipients",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("broadcast_id", sa.String(36), sa.ForeignKey("broadcasts.id"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
    )


def downgrade():
    op.drop_table("broadcast_recipients")
    op.drop_table("broadcasts")
    op.drop_table("vpn_clients")
    op.drop_table("notification_log")
    op.drop_table("referral_rewards")
    op.drop_column("devices", "platform")
    op.drop_constraint("fk_users_referred_by", "users", type_="foreignkey")
    op.drop_index("ix_users_referred_by_user_id", "users")
    op.drop_constraint("uq_users_referral_code", "users", type_="unique")
    for column in (
        "first_paid_at",
        "first_payment_discount_used_at",
        "referral_bonus_awarded_at",
        "referred_by_user_id",
        "referral_code",
    ):
        op.drop_column("users", column)
