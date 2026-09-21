"""M15:LLM 提示词 nonce 定界——随机后缀标签包裹不可信内容,
字面闭合标签(如 answer 里写 </answer>)无法提前结束数据块。"""
import secrets


def nonce_tag(name: str) -> str:
    return f"{name}-{secrets.token_hex(4)}"


def wrap(tag: str, text: str) -> str:
    return f"<{tag}>\n{text}\n</{tag}>"
