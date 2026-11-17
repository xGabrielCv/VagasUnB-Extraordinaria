"""Add composite index for fast vacancy-opening subscription fan-out."""

from alembic import op

revision = "0005"
down_revision = "0004"


def upgrade():
    op.create_index(
        "ix_subscription_match",
        "subscriptions",
        ["code", "period", "status"],
        if_not_exists=True,
    )


def downgrade():
    op.drop_index("ix_subscription_match", table_name="subscriptions", if_exists=True)
