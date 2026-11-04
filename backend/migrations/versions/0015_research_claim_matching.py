"""Public sources found for a criterion are linked to the claims they support.

Revision ID: 0015
Revises: 0014
"""

from app.migration_utils import run_sql_file

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0015_research_claim_matching.up.sql")


def downgrade() -> None:
    run_sql_file("0015_research_claim_matching.down.sql")
