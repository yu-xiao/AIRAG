# 检索评估集

每个知识库一个文件 `{kb_id}.json`,git 版本化。运行(backend 目录):

    py -m scripts.eval_retrieval --kb <kb_id> [--top-k 8] [--rerank] [--json]

字段:

- `question`:评估问题
- `expect_doc_ids`:期望命中的文档 id 列表(hit@k / MRR)
- `expect_keywords`:期望出现在 top-k 内容中的关键词(recall)
- `reference_answer`(可选,字符串):标准答案;eval_generation 会用它做一致性评审

示例见 M5 验收(T12)生成的样例文件。
