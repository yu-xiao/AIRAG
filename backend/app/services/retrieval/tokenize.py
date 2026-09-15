import jieba

jieba.initialize()  # 预热词典,避免首个请求卡顿


def tokenize(text: str) -> list[str]:
    return [t for t in jieba.cut_for_search(text) if t.strip()]
