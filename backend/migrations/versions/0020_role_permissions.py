"""Rights an administrator can grant to each user group.

Revision ID: 0020
Revises: 0019
"""

from app.migration_utils import run_sql_file

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0020_role_permissions.up.sql")


def downgrade() -> None:
    run_sql_file("0020_role_permissions.down.sql")
