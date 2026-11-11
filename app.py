# -*- coding: utf-8 -*-
"""
政策监测网站 (CloudBase 云托管版 v3)
- 页面与现行监测网站分享版同款 (template.html 由分享版改造: 数据/日期/统计改为服务端注入)
- 启动先载入 seed.json (分享版快照) 或 data.json (上次运行状态), 页面即刻可用;
  后台每小时抓取一次 (sources_v3 深抓), 新数据合并入库
- 发现日期口径: 每条带永久「首次发现日期」f (YYYY-MM-DD, 北京时间), URL 首次被看到即打戳、永不覆盖;
  「更新」页/专区与全库更新药丸在前端按 f 逐天分组、只增不删 (旧的按天清零口径废弃)
- f 随 data.json 落盘; 容器文件系统不持久, 重新部署/实例回收后以 seed + found_map.json 重建:
  found_map.json (部署包内, url->f) 记录部署前已核实的真实发现批次, 合并时以它与已有 f 为准 (最早者优先);
  全新 URL 按启发式打戳: 发布日期在最近 3 天内的打今日戳 (含停机/迁移后补抓的),
  更早的才算历史回填 (f 留空, 不进更新页) —— 2026-11-11 修正: 原「发布日期早于今天一律不打戳」
  会把停机期间发布、恢复后补抓到的条目误判为回填而吞掉发现日期 (实例: 2026年度第十一批
  创新型中小企业名单公告, 2026-11-10 发布, 实际抓到了却没进更新页)
- 路由: / 页面 / /data.json 当前数据 / /health 健康检查
- 监听端口: 环境变量 PORT, 没有就 80; 仅 Python 标准库
"""
import json
import os
import threading
import time
import datetime
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sources_v3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(BASE_DIR, "data.json")
FOUND_MAP_PATH = os.path.join(BASE_DIR, "found_map.json")
SEED_PATH = os.path.join(BASE_DIR, "seed.json")
TEMPLATE_PATH = os.path.join(BASE_DIR, "template.html")
REFRESH_SECONDS = 3600
CST = datetime.timezone(datetime.timedelta(hours=8))
DECLARE_ORG = "北京市政策兑现专区"

_lock = threading.Lock()
_state = {
    "items": {},          # url -> item (含 fs=first_seen 日期)
    "last_fetch_at": "",  # 最近一次抓取完成时间 (CST 字符串)
    "report": [],         # [{o,c,n,ok}]
    "fetching": False,
}

with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
    TEMPLATE = f.read()

# 部署前已核实的真实发现日期 (url -> f): 回填与防丢戳的权威来源之一, 最早者优先
FOUND_MAP = {}
if os.path.exists(FOUND_MAP_PATH):
    try:
        with open(FOUND_MAP_PATH, "r", encoding="utf-8") as f:
            FOUND_MAP = json.load(f)
        print("已载入 found_map.json: %d 条发现日期" % len(FOUND_MAP))
    except Exception as e:
        print("读取 found_map.json 失败: %s" % e)


def _earliest(*dates):
    ds = [d for d in dates if d]
    return min(ds) if ds else ""


def today_cst():
    return datetime.datetime.now(CST).date().isoformat()


def now_str():
    return datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def load_initial():
    """优先 data.json (含 first_seen), 其次 seed.json (分享版快照)"""
    if os.path.exists(DATA_PATH):
        try:
            with open(DATA_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            with _lock:
                _state["items"] = {it["u"]: it for it in d.get("items", [])}
                _state["last_fetch_at"] = d.get("last_fetch_at", "")
                _state["report"] = d.get("report", [])
            print("[%s] 已从 data.json 载入 %d 条" % (now_str(), len(_state["items"])))
            return
        except Exception as e:
            print("读取 data.json 失败, 改用 seed: %s" % e)
    if os.path.exists(SEED_PATH):
        try:
            with open(SEED_PATH, "r", encoding="utf-8") as f:
                seed = json.load(f)
            fs_map = seed.get("fs", {})
            items = {}
            for a in seed.get("articles", []):
                it = dict(a)
                it["fs"] = fs_map.get(it["u"], "")
                # f: 种子自带的真实发现批次与 found_map 取最早者; 无记录则留空 (不冒充)
                it["f"] = _earliest(fs_map.get(it["u"], ""), FOUND_MAP.get(it["u"], ""))
                it["_kind"] = "declare" if it.get("o") == DECLARE_ORG else "article"
                items[it["u"]] = it
            with _lock:
                _state["items"] = items
            print("[%s] 已从 seed.json 载入 %d 条" % (now_str(), len(items)))
        except Exception as e:
            print("读取 seed.json 失败: %s" % e)


def save_data():
    try:
        with _lock:
            payload = {
                "items": list(_state["items"].values()),
                "last_fetch_at": _state["last_fetch_at"],
                "report": _state["report"],
            }
        tmp = DATA_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, DATA_PATH)
    except Exception as e:
        print("写入 data.json 失败: %s" % e)


def do_fetch():
    with _lock:
        if _state["fetching"]:
            return
        _state["fetching"] = True
    try:
        print("[%s] 开始抓取..." % now_str())
        fetched, report = sources_v3.collect_all()
        today = today_cst()
        report_by_key = {(r["o"], r["c"]): r for r in report}
        with _lock:
            items = _state["items"]
            # 兑现专区: 整表替换 (它是现行登记库); 抓失败时保留旧数据。
            # 替换前先备份各条 first_seen 与发现日期 f, 替换后恢复, 避免整表被误标为今日新发现
            dec_fs_backup = {}
            dec_def = [d for d in sources_v3.SOURCE_DEFS if d[0] == DECLARE_ORG]
            if dec_def and report_by_key.get((DECLARE_ORG, "项目申报"), {}).get("ok"):
                for u in [u for u, it in items.items() if it.get("_kind") == "declare"]:
                    dec_fs_backup[u] = (items[u].get("fs", ""), items[u].get("f", ""))
                    del items[u]
            for it in fetched:
                u = it["u"]
                if u in items:
                    merged = dict(it)
                    merged["fs"] = items[u].get("fs", "")
                    # f 永不覆盖: 已有戳与 found_map 取最早者
                    merged["f"] = _earliest(items[u].get("f", ""), FOUND_MAP.get(u, ""))
                    items[u] = merged
                elif u in dec_fs_backup:
                    merged = dict(it)
                    merged["fs"], old_f = dec_fs_backup[u]
                    merged["f"] = _earliest(old_f, FOUND_MAP.get(u, ""))
                    items[u] = merged
                else:
                    # 第一次被本系统看到: 发布日期早于今天的算历史回填 (fs=发布日期);
                    # 发布于今天或无日期的, fs=今天
                    merged = dict(it)
                    d = merged.get("d")
                    merged["fs"] = d if (d and d < today) else today
                    # f 打戳: found_map 有记录用记录 (最早者优先的口径在已有条目分支处理);
                    # 无发布日期的打今日戳; 有发布日期的, 距今 3 天内 (含未来日期) 视为新发现打今日戳,
                    # 更早的留空 (历史回填不冒充新发现)。3 天窗口保证停机/迁移后补抓不丢戳,
                    # 又不让种子深抓的陈年旧文涌进更新页
                    fm = FOUND_MAP.get(u, "")
                    if fm:
                        merged["f"] = fm
                    elif not d:
                        merged["f"] = today
                    else:
                        try:
                            gap = (datetime.date.fromisoformat(today)
                                   - datetime.date.fromisoformat(d)).days
                        except Exception:
                            gap = 999
                        merged["f"] = today if gap <= 3 else ""
                    items[u] = merged
            _state["last_fetch_at"] = now_str()
            # report 的 n 改为库内现存条数 (含历史积累), 与页面 sources 展示一致
            counts = {}
            for it in items.values():
                k = (it.get("o", ""), it.get("c", ""))
                counts[k] = counts.get(k, 0) + 1
            new_report = []
            for org, column, _u in sources_v3.SOURCE_DEFS:
                r = report_by_key.get((org, column), {"ok": False})
                new_report.append({"o": org, "c": column,
                                   "n": counts.get((org, column), 0),
                                   "ok": bool(r.get("ok"))})
            _state["report"] = new_report
        save_data()
        print("[%s] 抓取完成: 库内共 %d 条" % (now_str(), len(_state["items"])))
    except Exception as e:
        print("抓取异常: %s" % e)
    finally:
        with _lock:
            _state["fetching"] = False


def refresh_loop():
    do_fetch()
    while True:
        time.sleep(REFRESH_SECONDS)
        do_fetch()


# ---------------- 类别 (fm): 全站监测后栏目近 90 个, 按类别归拢方便筛选 ----------------
FAMILY_BY_SOURCE = {}
for (_o, _n, _u, _e, _f, _p) in sources_v3.FULL_SOURCES:
    FAMILY_BY_SOURCE[(_o, _n)] = _f
FAMILY_BY_SOURCE[("工业和信息化部", "政策文件库")] = "政策文件"
FAMILY_BY_SOURCE[("工业和信息化部", "文件公示")] = "通知公告"
FAMILY_BY_SOURCE[("优质中小企业梯度培育平台", "部级公告")] = "通知公告"


def family_of(org, col):
    """(机构,栏目) -> 类别; 表里没有的 (历史栏目名) 按栏目名关键词兜底"""
    if org == DECLARE_ORG:
        return "申报项目"
    fam = FAMILY_BY_SOURCE.get((org, col))
    if fam:
        return fam
    c = col or ""
    if "解读" in c:
        return "政策解读"
    if any(k in c for k in ("数据", "统计", "经济运行")):
        return "数据发布"
    if any(k in c for k in ("征集", "访谈", "互动", "调查", "问题", "直播")):
        return "互动征集"
    if any(k in c for k in ("文件", "规划", "计划")):
        return "政策文件"
    if any(k in c for k in ("新闻", "动态", "要闻", "聚焦", "报道", "转载", "头条",
                            "高地", "展示", "举措", "活动", "之声", "发展", "合作",
                            "前沿", "改革", "园区", "转化", "发布")):
        return "新闻动态"
    return "通知公告"


# ---------------- 数据组装 (现行分享版 DATA 口径) ----------------

def build_data():
    today = today_cst()
    with _lock:
        items = list(_state["items"].values())
        report = list(_state["report"])
        last_fetch = _state["last_fetch_at"]
    arts, decs = [], []
    for it in items:
        (decs if it.get("_kind") == "declare" else arts).append(it)
    arts.sort(key=lambda a: (a.get("d") or "", a.get("fs") or ""), reverse=True)
    decs.sort(key=lambda a: (a.get("de") or ""), reverse=True)

    def pub(it):
        d = {k: v for k, v in it.items() if k not in ("fs", "_kind") and v is not None}
        d["fm"] = family_of(it.get("o", ""), it.get("c", ""))
        if not d.get("f"):
            d.pop("f", None)  # 无发现日期的条目不带 f 字段 (前端据此不进更新页)
        return d

    articles = [pub(a) for a in arts] + [pub(a) for a in decs]
    # 通知分组 = 全部带 f 的条目按发现日期逐天成组 (只增不删), 日期倒序
    by_found = {}
    for it in items:
        fv = it.get("f")
        if fv:
            by_found.setdefault(fv, []).append(it)
    notifications = []
    for fv in sorted(by_found.keys(), reverse=True):
        urls = [it["u"] for it in sorted(
            by_found[fv],
            key=lambda a: (a.get("d") or a.get("de") or ""), reverse=True)]
        notifications.append({"found": fv, "urls": urls})
    new_count = len(by_found.get(today, []))
    sources_list = []
    url_by_key = {(o, c): u for o, c, u in sources_v3.SOURCE_DEFS}
    if report:
        for r in report:
            sources_list.append({"o": r["o"], "c": r["c"],
                                 "u": url_by_key.get((r["o"], r["c"]), ""),
                                 "n": r["n"], "s": "ok" if r["ok"] else "fail",
                                 "fm": family_of(r["o"], r["c"])})
    else:
        counts = {}
        for it in items:
            k = (it.get("o", ""), it.get("c", ""))
            counts[k] = counts.get(k, 0) + 1
        for org, column, u in sources_v3.SOURCE_DEFS:
            sources_list.append({"o": org, "c": column, "u": u,
                                 "n": counts.get((org, column), 0), "s": "ok",
                                 "fm": family_of(org, column)})
    data = {"articles": articles, "sources": sources_list, "notifications": notifications}
    meta = {
        "total": len(articles),
        "declare_count": len(decs),
        "new_count": new_count,
        "today": today,
        "last_fetch": last_fetch,
        "ok_count": sum(1 for s in sources_list if s["s"] == "ok"),
        "src_total": len(sources_list),
    }
    return data, meta


def render_page():
    data, meta = build_data()
    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    today_cn = "%d月%d日" % (int(meta["today"][5:7]), int(meta["today"][8:10]))
    src_summary = "成功 %d 个、失败 %d 个" % (meta["ok_count"], meta["src_total"] - meta["ok_count"]) \
        if meta["last_fetch"] else "首轮抓取进行中"
    html = (TEMPLATE
            .replace("__DATA_JSON__", data_json)
            .replace("__TOTAL__", str(meta["total"]))
            .replace("__DECLARE_COUNT__", str(meta["declare_count"]))
            .replace("__NEW_COUNT__", str(meta["new_count"]))
            .replace("__TODAY_CN__", today_cn)
            .replace("__TODAY__", meta["today"])
            .replace("__YDAY__", (datetime.date.fromisoformat(meta["today"])
                                  - datetime.timedelta(days=1)).isoformat())
            .replace("__UPDATE_DATE__", meta["today"])
            .replace("__FETCH_TIME__", meta["last_fetch"] or "尚未完成首抓")
            .replace("__SRC_SUMMARY__", src_summary)
            .replace("__SRC_TOTAL__", str(meta["src_total"])))
    return html


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            self._send(b"ok", "text/plain; charset=utf-8")
            return
        if parsed.path == "/data.json":
            data, meta = build_data()
            body = json.dumps({"meta": meta, "data": data}, ensure_ascii=False).encode("utf-8")
            self._send(body, "application/json; charset=utf-8")
            return
        if parsed.path == "/":
            self._send(render_page().encode("utf-8"), "text/html; charset=utf-8")
            return
        self.send_response(404)
        self.end_headers()


def main():
    port = int(os.environ.get("PORT") or 80)
    load_initial()
    t = threading.Thread(target=refresh_loop, daemon=True)
    t.start()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("政策监测服务 v3 已启动, 监听端口 %d" % port)
    server.serve_forever()


if __name__ == "__main__":
    main()
