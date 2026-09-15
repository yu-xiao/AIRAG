from sqlalchemy import inspect

from app.models.base import Base


async def test_all_spec_tables_exist(prepare_db):
    names = set(inspect(Base.metadata).tables.keys())
    expected = {
        "users",
        "knowledge_bases",
        "kb_permissions",
        "documents",
        "chunks",
        "conversations",
        "messages",
    }
    assert expected <= names


def test_cleanup_order_covers_all_metadata_tables():
    from tests.conftest import CLEANUP_ORDER

    assert set(CLEANUP_ORDER) <= set(Base.metadata.tables)
