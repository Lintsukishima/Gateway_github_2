"""add traceability fields for messages and summaries

Revision ID: 6f8c0b1a2d34
Revises: 90edde7f4089
Create Date: 2026-02-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "6f8c0b1a2d34"
down_revision: Union[str, Sequence[str], None] = "90edde7f4089"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("thread_id", sa.String(), nullable=True))
    op.add_column("messages", sa.Column("memory_id", sa.String(), nullable=True))
    op.add_column("messages", sa.Column("agent_id", sa.String(), nullable=True))
    op.create_index(op.f("ix_messages_thread_id"), "messages", ["thread_id"], unique=False)
    op.create_index(op.f("ix_messages_memory_id"), "messages", ["memory_id"], unique=False)
    op.create_index(op.f("ix_messages_agent_id"), "messages", ["agent_id"], unique=False)

    for table in ("summaries_s4", "summaries_s60"):
        op.add_column(table, sa.Column("scope_type", sa.String(), nullable=True))
        op.add_column(table, sa.Column("thread_id", sa.String(), nullable=True))
        op.add_column(table, sa.Column("memory_id", sa.String(), nullable=True))
        op.add_column(table, sa.Column("agent_id", sa.String(), nullable=True))
        op.add_column(table, sa.Column("summary_version", sa.Integer(), nullable=True, server_default="1"))
        op.add_column(table, sa.Column("dedupe_key", sa.String(), nullable=True))

        op.create_index(op.f(f"ix_{table}_scope_type"), table, ["scope_type"], unique=False)
        op.create_index(op.f(f"ix_{table}_thread_id"), table, ["thread_id"], unique=False)
        op.create_index(op.f(f"ix_{table}_memory_id"), table, ["memory_id"], unique=False)
        op.create_index(op.f(f"ix_{table}_agent_id"), table, ["agent_id"], unique=False)
        op.create_index(op.f(f"ix_{table}_dedupe_key"), table, ["dedupe_key"], unique=False)


def downgrade() -> None:
    for table in ("summaries_s60", "summaries_s4"):
        op.drop_index(op.f(f"ix_{table}_dedupe_key"), table_name=table)
        op.drop_index(op.f(f"ix_{table}_agent_id"), table_name=table)
        op.drop_index(op.f(f"ix_{table}_memory_id"), table_name=table)
        op.drop_index(op.f(f"ix_{table}_thread_id"), table_name=table)
        op.drop_index(op.f(f"ix_{table}_scope_type"), table_name=table)

        op.drop_column(table, "dedupe_key")
        op.drop_column(table, "summary_version")
        op.drop_column(table, "agent_id")
        op.drop_column(table, "memory_id")
        op.drop_column(table, "thread_id")
        op.drop_column(table, "scope_type")

    op.drop_index(op.f("ix_messages_agent_id"), table_name="messages")
    op.drop_index(op.f("ix_messages_memory_id"), table_name="messages")
    op.drop_index(op.f("ix_messages_thread_id"), table_name="messages")
    op.drop_column("messages", "agent_id")
    op.drop_column("messages", "memory_id")
    op.drop_column("messages", "thread_id")
