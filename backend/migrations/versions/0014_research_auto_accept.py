"""Supporting findings from credible sources are accepted automatically.

Revision ID: 0014
Revises: 0013
"""

from app.migration_utils import run_sql_file

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0014_research_auto_accept.up.sql")


def downgrade() -> None:
    run_sql_file("0014_research_auto_accept.down.sql")
