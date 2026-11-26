"""Persist provider tasks and distinguish requested and executed modes."""

import sqlalchemy as sa
from alembic import op

revision = "0002_hybrid_generation"
down_revision = "0001_video_schema"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("video_jobs", sa.Column("execution_mode", sa.String(16)))
    op.add_column("video_jobs", sa.Column("fallback_reason", sa.String(64)))
    op.add_column(
        "video_jobs",
        sa.Column("ai_cost_uncertain", sa.Boolean(), server_default="false", nullable=False),
    )
    op.create_table(
        "video_ai_generations",
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("video_jobs.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("slot", sa.Integer(), primary_key=True),
        sa.Column("reference", sa.Text(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("endpoint", sa.String(255), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("resolution", sa.String(16), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        sa.Column("unit_price_usd", sa.Numeric(16, 8), nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(12, 4), nullable=False),
        sa.Column("actual_cost_usd", sa.Numeric(12, 4)),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("task_id", sa.String(128), unique=True),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("billing_uncertain", sa.Boolean(), nullable=False),
        sa.Column("transient_errors", sa.Integer(), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(64)),
        sa.CheckConstraint("slot IN (1, 2)", name="ck_video_ai_slot"),
    )
    op.create_index("ix_video_ai_generations_lease_until", "video_ai_generations", ["lease_until"])


def downgrade():
    op.drop_table("video_ai_generations")
    op.drop_column("video_jobs", "ai_cost_uncertain")
    op.drop_column("video_jobs", "fallback_reason")
    op.drop_column("video_jobs", "execution_mode")

