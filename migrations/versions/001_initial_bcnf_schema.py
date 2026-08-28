"""Initial BCNF Schema Migration.

Revision ID: 001_initial_bcnf_schema
Revises:
Create Date: 2026-08-28 22:15:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "001_initial_bcnf_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. users table
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("external_ref", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("external_ref", name=op.f("uq_users_external_ref")),
    )
    op.create_index(op.f("ix_users_external_ref"), "users", ["external_ref"], unique=True)

    # 2. providers table
    op.create_table(
        "providers",
        sa.Column("id", sa.SmallInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_providers")),
        sa.UniqueConstraint("code", name=op.f("uq_providers_code")),
    )
    op.create_index(op.f("ix_providers_code"), "providers", ["code"], unique=True)

    # 3. models table
    op.create_table(
        "models",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("provider_id", sa.SmallInteger(), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["providers.id"],
            name=op.f("fk_models_provider_id_providers"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_models")),
        sa.UniqueConstraint("provider_id", "model_name", name="uq_models_provider_model_name"),
    )
    op.create_index(
        "idx_models_provider_active", "models", ["provider_id", "active"], unique=False
    )

    # 4. challenges table
    op.create_table(
        "challenges",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="LIVE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'LIVE', 'DISABLED', 'ARCHIVED')",
            name="ck_challenges_status",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_challenges")),
        sa.UniqueConstraint("slug", name=op.f("uq_challenges_slug")),
    )
    op.create_index(op.f("ix_challenges_slug"), "challenges", ["slug"], unique=True)

    # 5. challenge_versions table
    op.create_table(
        "challenge_versions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("challenge_id", sa.BigInteger(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("system_prompt_ciphertext", sa.Text(), nullable=False),
        sa.Column("system_prompt_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["challenge_id"],
            ["challenges.id"],
            name=op.f("fk_challenge_versions_challenge_id_challenges"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_challenge_versions")),
        sa.UniqueConstraint(
            "challenge_id",
            "version_no",
            name="uq_challenge_versions_challenge_version",
        ),
    )
    op.create_index(
        "idx_challenge_versions_challenge",
        "challenge_versions",
        ["challenge_id", "version_no"],
        unique=False,
    )

    # 6. challenge_model_bindings table
    op.create_table(
        "challenge_model_bindings",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("challenge_version_id", sa.BigInteger(), nullable=False),
        sa.Column("model_id", sa.BigInteger(), nullable=False),
        sa.Column("max_input_tokens", sa.Integer(), nullable=False, server_default="2048"),
        sa.Column("max_output_tokens", sa.Integer(), nullable=False, server_default="512"),
        sa.Column(
            "temperature",
            sa.Numeric(precision=4, scale=3),
            nullable=False,
            server_default="0.700",
        ),
        sa.Column("timeout_ms", sa.Integer(), nullable=False, server_default="15000"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.CheckConstraint("max_input_tokens > 0", name="ck_cmb_max_input_tokens"),
        sa.CheckConstraint("max_output_tokens > 0", name="ck_cmb_max_output_tokens"),
        sa.CheckConstraint("temperature >= 0", name="ck_cmb_temperature"),
        sa.CheckConstraint("timeout_ms > 0", name="ck_cmb_timeout_ms"),
        sa.ForeignKeyConstraint(
            ["challenge_version_id"],
            ["challenge_versions.id"],
            name=op.f("fk_challenge_model_bindings_challenge_version_id_challenge_versions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["models.id"],
            name=op.f("fk_challenge_model_bindings_model_id_models"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_challenge_model_bindings")),
        sa.UniqueConstraint(
            "challenge_version_id",
            "model_id",
            name="uq_challenge_model_bindings_version_model",
        ),
    )

    # 7. runs table
    op.create_table(
        "runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("model_binding_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="QUEUED"),
        sa.Column("prompt_hash", sa.String(length=64), nullable=False),
        sa.Column("prompt_bytes", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', "
            "'PROVIDER_ERROR', 'TIMEOUT', 'ADMISSION_REJECTED', 'SYSTEM_ERROR')",
            name="ck_runs_status",
        ),
        sa.CheckConstraint("prompt_bytes >= 0", name="ck_runs_prompt_bytes"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_runs_attempt_count"),
        sa.ForeignKeyConstraint(
            ["model_binding_id"],
            ["challenge_model_bindings.id"],
            name=op.f("fk_runs_model_binding_id_challenge_model_bindings"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_runs_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
    )
    # Keyset pagination composite index
    op.create_index(
        "idx_runs_user_created_id", "runs", ["user_id", "created_at", "id"], unique=False
    )
    # Status monitoring / hot queue index
    op.create_index("idx_runs_status_created", "runs", ["status", "created_at"], unique=False)

    # 8. run_results table
    op.create_table(
        "run_results",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("response_object_key", sa.String(length=512), nullable=True),
        sa.Column("response_preview", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("finish_reason", sa.String(length=64), nullable=True),
        sa.Column("provider_request_id", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_run_results_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_run_results")),
    )


def downgrade() -> None:
    op.drop_table("run_results")
    op.drop_index("idx_runs_status_created", table_name="runs")
    op.drop_index("idx_runs_user_created_id", table_name="runs")
    op.drop_table("runs")
    op.drop_table("challenge_model_bindings")
    op.drop_index("idx_challenge_versions_challenge", table_name="challenge_versions")
    op.drop_table("challenge_versions")
    op.drop_index(op.f("ix_challenges_slug"), table_name="challenges")
    op.drop_table("challenges")
    op.drop_index("idx_models_provider_active", table_name="models")
    op.drop_table("models")
    op.drop_index(op.f("ix_providers_code"), table_name="providers")
    op.drop_table("providers")
    op.drop_index(op.f("ix_users_external_ref"), table_name="users")
    op.drop_table("users")
