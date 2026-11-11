"""Fence synthetic recipients independently of catalog and transport selection."""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"


def upgrade():
    op.add_column(
        "recipients", sa.Column("origin", sa.String(16), nullable=False, server_default="demo")
    )


def downgrade():
    raise RuntimeError("Do not remove the synthetic-recipient delivery fence")
