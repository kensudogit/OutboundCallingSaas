"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

★ 既に適用された環境があるリビジョンは書き換えない。変更は新しい
  リビジョンで行う。書き換えると、新規環境と既存環境でスキーマが
  食い違い、しかもどちらが正しいか分からなくなる。

★ RLS を有効にするテーブルを追加したら、ポリシーも同じリビジョンで
  作ること。テーブルだけ先に入ると、その間だけ他テナントから見える。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
