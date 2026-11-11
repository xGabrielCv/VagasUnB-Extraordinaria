"""Preserve source classroom identity when the public HTML provides it."""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"


def upgrade():
    op.add_column("classrooms", sa.Column("source_id", sa.String(80), nullable=True))


def downgrade():
    raise RuntimeError("Destructive downgrade disabled")
