"""Each rubric version has a rating scale: the original 1-5, or the board's 0-3.

Revision ID: 0018
Revises: 0017
"""

from app.migration_utils import run_sql_file

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0018_rating_scales.up.sql")


def downgrade() -> None:
    run_sql_file("0018_rating_scales.down.sql")
