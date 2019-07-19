"""add_jurisdiction_id_to_person

Revision ID: 16f89aa16e0e
Revises: 99d29cbb9841
Create Date: 2019-03-12 16:19:57.595458

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '16f89aa16e0e'
down_revision = '99d29cbb9841'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('person', sa.Column('jurisdiction_id', sa.String(length=255)))
    op.add_column('person_history',
                  sa.Column('jurisdiction_id', sa.String(length=255)))
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column('person_history', 'jurisdiction_id')
    op.drop_column('person', 'jurisdiction_id')
    # ### end Alembic commands ###