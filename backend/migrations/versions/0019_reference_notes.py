"""Notes from a finalist's references, up to four per nomination.

Revision ID: 0019
Revises: 0018
"""

from app.migration_utils import run_sql_file

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0019_reference_notes.up.sql")


def downgrade() -> None:
    run_sql_file("0019_reference_notes.down.sql")
