"""Enforce unique skill names in the shared catalog and per private owner."""
from alembic import context, op
import sqlalchemy as sa

revision = "0024_skill_names"
down_revision = "0023_persona_skills"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not context.is_offline_mode():
        duplicates = op.get_bind().execute(sa.text("""
            SELECT 1 FROM skills
            GROUP BY shared, CASE WHEN shared THEN NULL ELSE user_id END, name
            HAVING COUNT(*) > 1 LIMIT 1
        """)).first()
        if duplicates:
            raise RuntimeError("Duplicate skill names exist in a private or shared catalog. Resolve them before upgrading.")
    op.create_index("uq_skills_shared_name", "skills", ["name"], unique=True, postgresql_where=sa.text("shared"))
    op.create_index("uq_skills_private_name", "skills", ["user_id", "name"], unique=True, postgresql_where=sa.text("NOT shared"))


def downgrade() -> None:
    op.drop_index("uq_skills_private_name", table_name="skills")
    op.drop_index("uq_skills_shared_name", table_name="skills")
