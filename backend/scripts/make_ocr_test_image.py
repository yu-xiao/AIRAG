# -*- coding: utf-8 -*-
"""生成一张带文档文字的测试图片,供手动验证 OCR 链路(用 pymupdf 渲染,免 PIL)。"""
from pathlib import Path

import pymupdf as fitz

OUT = Path(r"C:\Users\Administrator\Pictures\ocr_test.png")

doc = fitz.open()
page = doc.new_page(width=612, height=792)  # 72dpi 点位,渲染时放大
page.insert_text((50, 70), "量子计算研究进展报告", fontsize=20, fontname="china-s")
lines = [
    "一、研究背景",
    "量子计算机利用量子比特的叠加与纠缠特性,",
    "在特定问题上可实现指数级加速。",
    "二、本期成果",
    "实验室完成 56 比特超导量子芯片封装,",
    "门保真度达到 99.7%,相干时间提升至 200 微秒。",
    "三、下一步计划",
    "2026 年第四季度开展化学模拟场景验证,",
    "与合作单位共建量子云平台试用环境。",
]
y = 120
for ln in lines:
    page.insert_text((50, y), ln, fontsize=13, fontname="china-s")
    y += 32
pix = page.get_pixmap(matrix=fitz.Matrix(2.2, 2.2))  # ~158dpi,文字清晰
OUT.parent.mkdir(parents=True, exist_ok=True)
pix.save(str(OUT))
print(f"saved: {OUT} ({pix.width}x{pix.height})")
