async def test_admin_and_owner_are_implicit_owner(db_session):
    from app.core.perms import get_kb_perm
    from app.models import KnowledgeBase, User

    owner = User(username="p_owner", password_hash="x", role="editor")
    admin = User(username="p_admin", password_hash="x", role="admin")
    db_session.add_all([owner, admin])
    await db_session.flush()
    kb = KnowledgeBase(name="权限库", owner_id=owner.id)
    db_session.add(kb)
    await db_session.flush()
    assert await get_kb_perm(db_session, admin, kb) == "owner"
    assert await get_kb_perm(db_session, owner, kb) == "owner"


async def test_granted_and_missing_perm(db_session):
    from app.core.perms import get_kb_perm
    from app.models import KnowledgeBase, KbPermission, User

    owner = User(username="g_owner", password_hash="x", role="editor")
    viewer = User(username="g_viewer", password_hash="x", role="viewer")
    stranger = User(username="g_other", password_hash="x", role="editor")
    db_session.add_all([owner, viewer, stranger])
    await db_session.flush()
    kb = KnowledgeBase(name="授权库", owner_id=owner.id)
    db_session.add(kb)
    await db_session.flush()
    db_session.add(KbPermission(kb_id=kb.id, user_id=viewer.id, perm="viewer"))
    await db_session.flush()
    assert await get_kb_perm(db_session, viewer, kb) == "viewer"
    assert await get_kb_perm(db_session, stranger, kb) is None


def test_has_perm_ranking():
    from app.core.perms import has_perm

    assert has_perm("editor", "viewer")
    assert has_perm("owner", "editor")
    assert not has_perm("viewer", "editor")
    assert not has_perm(None, "viewer")


async def test_register_defaults_to_viewer(client):
    resp = await client.post(
        "/api/auth/register", json={"username": "plain_me", "password": "secret123"}
    )
    assert resp.status_code == 201
    assert resp.json()["role"] == "viewer"
