# -*- coding: utf-8 -*-
"""
build_inline.py —— 把 ECharts 内联进 index.html，产出完全自包含的单文件页面

为什么需要：
  WorkBuddy 预览面板 / 第三方静态代理有 CSP 限制
  （`script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: data:`），
  跨源加载外部 JS 会被拦截；同时本机 CDN 又不可达。
  只有把 echarts 直接内联进 HTML，才能在任意入口下都正常渲染图表。

做法：
  读取 index.template.html，把 <script src="./vendor/echarts.min.js"></script>
  替换为内联的 <script>…</script>，输出 index.html。
  开发时改模板，改完跑一次本脚本重新生成。
"""
import os
import re

BASE = os.path.dirname(os.path.abspath(__file__))
TPL = os.path.join(BASE, "index.template.html")
OUT = os.path.join(BASE, "index.html")
ECHARTS = os.path.join(BASE, "vendor", "echarts.min.js")

PLACEHOLDER = '<script src="./vendor/echarts.min.js"></script>'
INLINE_HEAD = ('<!-- ECharts 已内联（避免 CSP / 跨源限制导致图表无法加载） -->\n'
               '<script>\n')


def main():
    if not os.path.exists(TPL):
        raise SystemExit("缺少模板: %s" % TPL)
    if not os.path.exists(ECHARTS):
        raise SystemExit("缺少 ECharts: %s" % ECHARTS)

    with open(TPL, encoding="utf-8") as f:
        tpl = f.read()
    with open(ECHARTS, encoding="utf-8") as f:
        ech = f.read()

    if "</script" in ech.lower():
        raise SystemExit("ECharts 源码含 </script，内联会提前闭合标签")

    if PLACEHOLDER not in tpl:
        raise SystemExit("模板中未找到 ECharts 占位符")

    inline = INLINE_HEAD + ech + "\n</script>"
    html = tpl.replace(PLACEHOLDER, inline)

    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)

    print("已生成自包含页面: %s (%.1f KB)" % (OUT, len(html.encode("utf-8")) / 1024))


if __name__ == "__main__":
    main()
