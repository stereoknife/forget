"""empty message

Revision ID: f11fe22d6169
Revises: 0cb99099c2dd
Create Date: 2017-07-31 01:07:39.741008

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f11fe22d6169'
down_revision = '0cb99099c2dd'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('twitter_archives', sa.Column('chunks', sa.Integer(), nullable=True))
    op.add_column('twitter_archives', sa.Column('chunks_failed', sa.Integer(), server_default='0', nullable=True))
    op.add_column('twitter_archives', sa.Column('chunks_successful', sa.Integer(), server_default='0', nullable=True))
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column('twitter_archives', 'chunks_successful')
    op.drop_column('twitter_archives', 'chunks_failed')
    op.drop_column('twitter_archives', 'chunks')
    # ### end Alembic commands ###