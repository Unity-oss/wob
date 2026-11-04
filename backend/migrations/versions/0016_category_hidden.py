"""Award categories can be hidden from lists, such as a test category.

Revision ID: 0016
Revises: 0015
"""

from app.migration_utils import run_sql_file

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0016_category_hidden.up.sql")


def downgrade() -> None:
    run_sql_file("0016_category_hidden.down.sql")
