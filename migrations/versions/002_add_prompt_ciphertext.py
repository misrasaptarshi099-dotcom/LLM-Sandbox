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
    # 1. Add prompt_ciphertext as nullable first for existing rows
    op.add_column(
        "runs",
        sa.Column("prompt_ciphertext", sa.Text(), nullable=True),
    )
    # 2. Transition pre-migration non-terminal runs to SYSTEM_ERROR
    # because they cannot be executed without participant prompt ciphertext.
    op.execute(
        "UPDATE runs SET status = 'SYSTEM_ERROR', finished_at = CURRENT_TIMESTAMP "
        "WHERE prompt_ciphertext IS NULL AND status IN ('QUEUED', 'RUNNING')"
    )
    # 3. Backfill existing historical runs with a valid encrypted tombstone ciphertext
    # so workers/tools do not encounter corrupt/empty ciphertext strings.
    from app.core.security import encrypt_system_prompt

    tombstone = encrypt_system_prompt("[UNAVAILABLE_HISTORICAL_RUN]")
    bind = op.get_bind()
    bind.execute(
        sa.text("UPDATE runs SET prompt_ciphertext = :tombstone WHERE prompt_ciphertext IS NULL"),
        {"tombstone": tombstone},
    )
    op.alter_column("runs", "prompt_ciphertext", nullable=False)


def downgrade() -> None:
    op.drop_column("runs", "prompt_ciphertext")
