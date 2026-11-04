"""Claims supported by a supporting document are verified automatically (jury decision).

Revision ID: 0017
Revises: 0016
"""

from app.migration_utils import run_sql_file

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0017_jury_verified_claims.up.sql")


def downgrade() -> None:
    run_sql_file("0017_jury_verified_claims.down.sql")
