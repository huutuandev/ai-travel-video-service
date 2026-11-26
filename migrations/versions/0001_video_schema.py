"""Create video jobs, scenes, assets and durable dispatch records."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM

revision = "0001_video_schema"
down_revision = None
branch_labels = None
depends_on = None

mode = ENUM("LOCAL", "HYBRID", name="video_mode", create_type=False)
status = ENUM(
    "QUEUED", "PROCESSING", "RENDERING", "COMPLETED", "FAILED", "CANCELLED",
    name="video_status", create_type=False,
)


def upgrade():
    mode.create(op.get_bind())
    status.create(op.get_bind())
    op.create_table(
        "video_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.String(128), nullable=False),
        sa.Column("mode", mode, nullable=False),
        sa.Column("status", status, nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        sa.Column("aspect_ratio", sa.String(8), nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(12, 4)),
        sa.Column("actual_cost_usd", sa.Numeric(12, 4), nullable=False),
        sa.Column("output_url", sa.Text()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("request_payload", sa.JSON(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.UniqueConstraint("tenant_id", "request_id", name="uq_video_jobs_tenant_request"),
        sa.CheckConstraint("progress BETWEEN 0 AND 100", name="ck_video_jobs_progress"),
    )
    op.create_index("ix_video_jobs_tenant_created", "video_jobs", ["tenant_id", "created_at", "id"])
    op.create_table(
        "video_scenes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("video_jobs.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("duration_seconds", sa.Numeric(8, 3), nullable=False),
        sa.Column("provider", sa.String(64)),
        sa.Column("prompt", sa.Text()),
        sa.Column("status", status, nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 4), nullable=False),
        sa.UniqueConstraint("job_id", "position", name="uq_video_scenes_job_position"),
    )
    op.create_table(
        "video_assets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("video_jobs.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("storage_backend", sa.String(32), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
    )
    op.create_index("ix_video_assets_job_id", "video_assets", ["job_id"])
    op.create_table(
        "video_dispatches",
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("video_jobs.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_dispatch_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_video_dispatches_available_at", "video_dispatches", ["available_at"])
    op.create_index(
        "ix_video_dispatches_next_dispatch_at", "video_dispatches", ["next_dispatch_at"]
    )


def downgrade():
    op.drop_table("video_dispatches")
    op.drop_table("video_assets")
    op.drop_table("video_scenes")
    op.drop_table("video_jobs")
    status.drop(op.get_bind())
    mode.drop(op.get_bind())

