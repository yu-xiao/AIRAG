class _Resp:
    def __init__(self, text):
        self.content = text


class _LLMScript:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def ainvoke(self, msgs, config=None):
        self.calls.append(msgs)
        return _Resp(self.replies.pop(0))


async def test_judge_parses_and_clamps():
    from app.services.eval_judge import faithfulness_score, relevancy_score

    f = await faithfulness_score(
        _LLMScript(['{"score": 1.7, "reasons": "全有依据"}']),
        "预算多少", "三千万[1]", ["预算三千万"],
    )
    assert f == {"score": 1.0, "reasons": "全有依据"}
    v = await relevancy_score(
        _LLMScript(['```json\n{"score": 0.42, "reasons": "部分回答"}\n```']),
        "预算多少", "不清楚",
    )
    assert v["score"] == 0.42


async def test_judge_retries_once_then_none():
    from app.services.eval_judge import relevancy_score

    ok = await relevancy_score(
        _LLMScript(["不是 json", '{"score": 0.8}']), "q", "a"
    )
    assert ok["score"] == 0.8
    bad = await relevancy_score(
        _LLMScript(["坏输出", "还是坏"]), "q", "a"
    )
    assert bad["score"] is None


async def test_reference_score_parses_and_clamps():
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.services.eval_judge import reference_score

    llm = FakeListChatModel(responses=['{"score": 1.7, "reasons": "一致"}'])
    out = await reference_score(llm, "预算?", "三千万元", "约三千万元")
    assert out["score"] == 1.0 and out["reasons"] == "一致"


async def test_judge_prompt_shape():
    from app.services.eval_judge import faithfulness_score, relevancy_score

    fl = _LLMScript(['{"score": 1.0}'])
    await faithfulness_score(fl, "q", "a", ["参考资料甲"])
    assert "参考资料" in fl.calls[0][1][1]  # user 消息带上下文
    rl = _LLMScript(['{"score": 1.0}'])
    await relevancy_score(rl, "q", "a")
    assert "参考资料" not in rl.calls[0][1][1]  # 切题度只看问题与答案
