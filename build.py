# -*- coding: utf-8 -*-
"""
政策监测网站 (GitHub 版) 一次性构建脚本
- 复用 CloudBase 版 app.py 的全部逻辑 (载入/抓取合并/发现日期打戳/页面渲染), 只把常驻服务换成单次运行:
  1) load_initial(): 优先 data.json (仓库持久化状态), 首次用 seed.json + found_map.json
  2) do_fetch():     抓取 14 个来源并合并入库 (内部已调用 save_data 写回 data.json)
  3) render_page():  生成完整静态页 (数据/日期/统计全部内联) -> docs/index.html
- 由 GitHub Actions 每小时运行一次, 产物 (data.json + docs/index.html) 提交回仓库,
  GitHub Pages 直接发布 docs/ —— 数据与发现日期戳随 git 历史永久保存, 不依赖任何常驻服务器
- 仅 Python 标准库
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.chdir(BASE)

import app  # noqa: E402


def main():
    app.load_initial()
    app.do_fetch()
    html = app.render_page()
    docs = os.path.join(BASE, "docs")
    os.makedirs(docs, exist_ok=True)
    out = os.path.join(docs, "index.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    _data, meta = app.build_data()
    print("BUILD OK total=%d declare=%d new_today=%d sources_ok=%d/%d last_fetch=%s -> %s (%d bytes)" % (
        meta["total"], meta["declare_count"], meta["new_count"],
        meta["ok_count"], meta["src_total"], meta["last_fetch"],
        out, len(html.encode("utf-8"))))


if __name__ == "__main__":
    main()
