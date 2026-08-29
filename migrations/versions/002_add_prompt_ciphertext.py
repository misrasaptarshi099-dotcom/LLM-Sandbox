"""Add prompt_ciphertext column to runs table.

Revision ID: 002_add_prompt_ciphertext
Revises: 001_initial_bcnf_schema
Create Date: 2026-08-29 11:00:00.000000

Phase 6: The worker needs the encrypted participant prompt to construct
the SYSTEM + USER LLM request. Stored as AES-256-GCM ciphertext (Rule §2).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "002_add_prompt_ciphertext"
down_revision: str | None = "001_initial_bcnf_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add prompt_ciphertext as nullable first for existing rows,
    # then backfill and set NOT NULL.
    op.add_column(
        "runs",
        sa.Column("prompt_ciphertext", sa.Text(), nullable=True),
    )
    # For any existing rows, set a placeholder (these runs cannot be re-executed).
    op.execute("UPDATE runs SET prompt_ciphertext = '' WHERE prompt_ciphertext IS NULL")
    op.alter_column("runs", "prompt_ciphertext", nullable=False)


def downgrade() -> None:
    op.drop_column("runs", "prompt_ciphertext")
