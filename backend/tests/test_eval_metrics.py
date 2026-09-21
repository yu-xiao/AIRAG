from app.services.eval_runner import hit_at_k, keyword_recall, mrr


def test_hit_at_k():
    assert hit_at_k([5, 9, 2], [2]) is True
    assert hit_at_k([5, 9], [2]) is False
    assert hit_at_k([], [1]) is False
    assert hit_at_k([1], []) is False


def test_mrr():
    assert mrr([3, 7], [3]) == 1.0
    assert mrr([5, 3, 7], [3]) == 0.5
    assert mrr([1, 2], [9]) == 0.0


def test_keyword_recall():
    assert keyword_recall(["预算三千万元"], ["预算", "三千"]) == 1.0
    assert keyword_recall(["预算三千万元"], ["预算", "负责人"]) == 0.5
    assert keyword_recall(["内容"], []) == 1.0
    assert keyword_recall([], ["任何"]) == 0.0
