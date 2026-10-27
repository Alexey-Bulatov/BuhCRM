"""Allow unassigned tasks while rejecting two simultaneous assignment targets.

The historical aa8904c1e96f revision was empty, despite its description.
"""
from alembic import op
import sqlalchemy as sa

revision = "b20261027_task_target"
down_revision = "aa8904c1e96f"
branch_labels = None
depends_on = None


def _drop_assignment_checks():
    names = {"ck_task_ck_task_one_target", "ck_task_one_target",
             "ck_task_target_at_most_one", "ck_task_ck_task_target_at_most_one"}
    for check in sa.inspect(op.get_bind()).get_check_constraints("task"):
        if check["name"] in names:
            op.drop_constraint(op.f(check["name"]), "task", type_="check")


def upgrade():
    _drop_assignment_checks()
    op.create_check_constraint(op.f("ck_task_target_at_most_one"), "task",
                               "department_id IS NULL OR assignee_user_id IS NULL")


def downgrade():
    # PostgreSQL rejects the downgrade if unassigned tasks still exist; no data is deleted.
    _drop_assignment_checks()
    op.create_check_constraint(op.f("ck_task_ck_task_one_target"), "task",
                               "(department_id IS NOT NULL) <> (assignee_user_id IS NOT NULL)")
