"""Judges' independent scores and comments, and the rights to score and to see the tally.

Revision ID: 0021
Revises: 0020
"""

from app.migration_utils import run_sql_file

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0021_judge_scores.up.sql")


def downgrade() -> None:
    run_sql_file("0021_judge_scores.down.sql")
