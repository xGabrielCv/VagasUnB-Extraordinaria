"""Add composite and foreign-key indexes for concurrent user and fan-out scaling."""

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
    op.create_index(
        "ix_outbox_subscription_id",
        "outbox",
        ["subscription_id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_callbacks_expires",
        "callbacks",
        ["expires"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_classroom_period_campus",
        "classrooms",
        ["period", "campus"],
        if_not_exists=True,
    )


def downgrade():
    op.drop_index("ix_classroom_period_campus", table_name="classrooms", if_exists=True)
    op.drop_index("ix_callbacks_expires", table_name="callbacks", if_exists=True)
    op.drop_index("ix_outbox_subscription_id", table_name="outbox", if_exists=True)
    op.drop_index("ix_subscription_match", table_name="subscriptions", if_exists=True)
