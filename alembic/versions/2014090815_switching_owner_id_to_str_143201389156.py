"""switching owner_id to str

Revision ID: 143201389156
Revises: 136275e06649
Create Date: 2014-09-08 15:15:45.393535

"""

# revision identifiers, used by Alembic.
revision = '143201389156'
down_revision = '136275e06649'

from alembic import op
import sqlalchemy as sa


def upgrade():
    ### commands auto generated by Alembic - please adjust! ###
    op.alter_column("bundle", sa.Column('owner_id', sa.String(length=255), nullable=True))
    op.alter_column("worksheet", sa.Column('owner_id', sa.String(length=255), nullable=True))
    op.alter_column("group", sa.Column('owner_id', sa.String(length=255), nullable=True))
    ### end Alembic commands ###


def downgrade():
    ### commands auto generated by Alembic - please adjust! ###
    op.alter_column("bundle", sa.Column('owner_id', sa.Integer, nullable=True))
    op.alter_column("worksheet", sa.Column('owner_id', sa.Integer, nullable=True))
    op.alter_column("group", sa.Column('owner_id', sa.Integer, nullable=True))
    ### end Alembic commands ###