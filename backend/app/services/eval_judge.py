"""M6 生成质量 LLM-judge:faithfulness(忠实度)与 answer relevancy(切题度)。

与 RAGAS 的取舍:只要两个指标,自研中文 prompt 零依赖、输出可控
(spec §2.1);坏输出重试一次,仍坏记 None 不伪装成 0。
"""
import json

from loguru import logger

from app.services.chat_graph.nodes import _extract_json

FAITHFULNESS_SYSTEM = (
    "你是答案忠实度评审。对照参考资料判断答案中的陈述是否都有依据:"
    "全部有依据接近1.0,部分有依据取中间值,存在编造接近0.0;"
    '答案明确表示"知识库中未找到相关内容"类拒答时按1.0。'
    '只输出 JSON:{"score": <0.0~1.0 的数值>, "reasons": "<一句话依据>"}'
)

RELEVANCY_SYSTEM = (
    "你是答案切题度评审。判断答案是否直接回答了所问问题:"
    "完整回答接近1.0,部分回答取中间值,答非所问接近0.0;"
    "问题确实无法回答且答案礼貌拒答时按0.5。"
    '只输出 JSON:{"score": <0.0~1.0 的数值>, "reasons": "<一句话依据>"}'
)


async def _judge(llm, system: str, user: str) -> dict:
    for _ in range(2):  # 坏输出重试一次
        try:
            resp = await llm.ainvoke([("system", system), ("user", user)])
            parsed = json.loads(_extract_json(resp.content))
            score = max(0.0, min(1.0, float(parsed["score"])))
            return {"score": score, "reasons": str(parsed.get("reasons", ""))[:200]}
        except Exception:
            logger.warning("judge output unparseable, retrying")
    return {"score": None, "reasons": "parse failed"}


async def faithfulness_score(llm, question: str, answer: str, contexts: list[str]) -> dict:
    ctx = "\n\n".join(f"[{i + 1}] {c}" for i, c in enumerate(contexts))
    user = f"问题:{question}\n参考资料:\n{ctx}\n答案:{answer}"
    return await _judge(llm, FAITHFULNESS_SYSTEM, user)


async def relevancy_score(llm, question: str, answer: str) -> dict:
    user = f"问题:{question}\n答案:{answer}"
    return await _judge(llm, RELEVANCY_SYSTEM, user)


REFERENCE_SYSTEM = (
    "你是答案一致性评审。对照参考答案判断回答是否覆盖了参考答案的关键事实:"
    "关键事实完整一致接近1.0,部分覆盖取中间值,存在明显冲突或遗漏接近0.0;"
    '回答明确表示无法从资料回答时按0.0。'
    '只输出 JSON:{"score": <0.0~1.0 的数值>, "reasons": "<一句话依据>"}'
)


async def reference_score(llm, question: str, answer: str,
                          reference: str) -> dict:
    user = f"问题:{question}\n参考答案:{reference}\n回答:{answer}"
    return await _judge(llm, REFERENCE_SYSTEM, user)
