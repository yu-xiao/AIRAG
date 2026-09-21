# backend/tests/test_eval_cli.py
"""M14:eval CLI 装配路径(_amain 单 asyncio.run)与 saved 行 stderr。"""
import json
import sys


def test_eval_retrieval_cli_main(monkeypatch, capfd):
    import scripts.eval_retrieval as cli

    async def fake_run(kb, top_k, rerank):
        return [{"question": "q", "expect_doc_ids": [1],
                 "expect_keywords": [], "hit_at_k": True,
                 "mrr": 1.0, "keyword_recall": 1.0}]

    saved = {}

    async def fake_save_run(kb_id, mode, results):
        saved.update(kb_id=kb_id, mode=mode, n=len(results))
        return 42

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr("scripts.eval_store.save_run", fake_save_run)
    monkeypatch.setattr(
        sys, "argv", ["eval_retrieval.py", "--kb", "3", "--save", "--json"])
    cli.main()
    out, err = capfd.readouterr()
    assert "saved: run_id=42" in err        # C5:进 stderr
    assert json.loads(out)[0]["question"] == "q"  # stdout 纯 JSON
    assert saved == {"kb_id": 3, "mode": "retrieval", "n": 1}


def test_eval_generation_cli_main(monkeypatch, capfd):
    import scripts.eval_generation as cli

    async def fake_run(kb, rerank):
        return [{"question": "q", "answer": "a", "refused": False,
                 "faithfulness": {"score": 0.9}, "relevancy": {"score": 0.8},
                 "citations": 1}]

    saved = {}

    async def fake_save_run(kb_id, mode, results):
        saved.update(kb_id=kb_id, mode=mode, n=len(results))
        return 7

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr("scripts.eval_store.save_run", fake_save_run)
    monkeypatch.setattr(
        sys, "argv", ["eval_generation.py", "--kb", "3", "--save", "--json"])
    cli.main()
    out, err = capfd.readouterr()
    assert "saved: run_id=7" in err
    assert json.loads(out)[0]["answer"] == "a"
    assert saved == {"kb_id": 3, "mode": "generation", "n": 1}
