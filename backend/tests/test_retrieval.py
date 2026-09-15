from app.services.retrieval.tokenize import tokenize


def test_tokenize_splits_chinese():
    tokens = tokenize("企业知识库的检索质量")
    assert isinstance(tokens, list) and len(tokens) >= 3
    assert all(t.strip() for t in tokens)
    assert any("知识库" in t for t in tokens)


def test_rrf_fusion_ranks_common_top():
    from app.services.retrieval.searcher import rrf_fuse

    vec = [("a", 0.9), ("b", 0.8), ("c", 0.7)]  # (chunk_id, raw)
    kw = [("b", 3.2), ("d", 2.0), ("a", 1.1)]
    fused = rrf_fuse(vec, kw, k=60)
    top = fused[0][0]
    assert top == "b"  # 双榜均在前列
    ids = [i for i, _ in fused]
    assert set(ids) == {"a", "b", "c", "d"}


async def test_hybrid_search_returns_matching_chunk(client, auth_headers, db_session):
    from app.models import Chunk, Document, KnowledgeBase
    from app.services.retrieval.searcher import hybrid_search

    # 增补 3:owner_id=1 在用户序列推进后不存在(FK 失败),
    # 改从 /api/auth/me 取 auth_headers 注册用户的真实 id,断言不变。
    me = (await client.get("/api/auth/me", headers=auth_headers)).json()
    user_rows = await db_session.execute(
        KnowledgeBase.__table__.insert()
        .values(name="检索测试库", owner_id=me["id"])
        .returning(KnowledgeBase.id)
    )
    kb_id = user_rows.scalar_one()
    doc = Document(
        kb_id=kb_id, filename="r.pdf", file_path="x", mime="application/pdf",
        size=1, sha256="r" * 64,
    )
    db_session.add(doc)
    await db_session.flush()
    from app.services.embedding.fake import FakeEmbedding

    vec = FakeEmbedding().embed_documents(["企业差旅报销流程规定"])[0]
    db_session.add(
        Chunk(document_id=doc.id, kb_id=kb_id, chunk_index=0,
              content="企业差旅报销流程规定:三级审批,七个工作日到账", page_no=1,
              char_len=24, embedding=vec, content_hash="h1")
    )
    db_session.add(
        Chunk(document_id=doc.id, kb_id=kb_id, chunk_index=1,
              content="完全无关的另一个主题内容关于天气", page_no=2,
              char_len=17, embedding=FakeEmbedding().embed_documents(["天气"])[0],
              content_hash="h2")
    )
    await db_session.flush()
    await db_session.execute(
        __import__("sqlalchemy").text(
            "UPDATE chunks SET tsv = to_tsvector('simple', :t) WHERE document_id = :d"
        ).bindparams(t="企业 差旅 报销 流程 规定 三级 审批", d=doc.id)
    )
    await db_session.commit()

    hits = await hybrid_search(db_session, [kb_id], "差旅报销怎么走")
    assert len(hits) >= 1
    assert hits[0].chunk_index if hasattr(hits[0], "chunk_index") else True
    assert "差旅" in hits[0].content
