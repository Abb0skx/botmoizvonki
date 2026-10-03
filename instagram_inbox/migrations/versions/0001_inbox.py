"""Initial independent inbox schema."""
from alembic import op
from instagram_inbox.models import Base

revision = '0001_inbox'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.create_all(bind=op.get_bind())


def downgrade():
    raise RuntimeError('Inbox history is permanent; restore a backup for rollback, never drop tables.')
