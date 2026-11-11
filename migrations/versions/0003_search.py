"""Indexed local search, without catalog copies or an external search service."""

from alembic import op

revision = "0003"
down_revision = "0002"


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE INDEX ix_classroom_name_trgm ON classrooms USING gin (search gin_trgm_ops)")
    op.execute(
        r"CREATE INDEX ix_classroom_code_key ON classrooms (regexp_replace(lower(code), '([a-z]+)0+([0-9])', '\1\2', 'g') text_pattern_ops)"
    )


def downgrade():
    op.execute("DROP INDEX ix_classroom_code_key")
    op.execute("DROP INDEX ix_classroom_name_trgm")
