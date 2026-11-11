# -*- coding: utf-8 -*-
"""
政策监测抓取 v3 (CloudBase 云托管版)
- 在 sources.py (与正式站同源抓取) 基础上改深抓:
  * 政府栏目翻页: index_N.html (发改委为 index_N.htm), 每栏目最多 MAX_PAGES 页,
    某页无新增即停
  * 通州区要闻动态: 聚合页 + 5 个子频道首页合并去重
  * 梯度培育平台: 修正接口解析 (响应在 data.pageContent, 参数 page/pageSize), 翻完 pageCount
  * 政策兑现专区: 全量分页 (沿用 sources.scrape_declare, 字段改现行分享版口径)
  * 工信部政策文件库: 站群搜索接口取 300 条 (沿用 sources.scrape_miit_search)
- 输出 item 为现行分享版 DATA 的字段口径:
  普通文章 {t,u,o,c,d,fn?}; 兑现专区 {t,u,o,c,rg,gv,st,de,mm?}; 另加内部字段 _kind
"""
import re
import time
import json
import ssl
import urllib.request
import urllib.parse
import http.cookiejar

import sources as base

MAX_PAGES = 6          # 每个栏目最多翻页数 (含首页)
PAGE_LIMIT = 60        # parse_list_page 单页上限

# (机构[分享版口径], 栏目, 列表页, 分页后缀)
DEEP_SOURCES = [
    ("北京市经济和信息化局", "通知公告", "https://jxj.beijing.gov.cn/jxdt/tzgg/", ".html"),
    ("北京市经济和信息化局", "政策文件", "https://jxj.beijing.gov.cn/zwgk/2024zcwj/", ".html"),
    ("北京市经济和信息化局", "办事通知", "https://jxj.beijing.gov.cn/bsfw/bstz/", ".html"),
    ("北京市科学技术委员会", "通知公告", "https://kw.beijing.gov.cn/zwgk/tzgg/", ".html"),
    ("北京市科学技术委员会", "政策文件", "https://kw.beijing.gov.cn/zwgk/zcwj/", ".html"),
    ("北京市发展和改革委员会", "通知通告", "https://fgw.beijing.gov.cn/gzdt/tztg/", ".htm"),
    ("北京市发展和改革委员会", "政策文件", "https://fgw.beijing.gov.cn/fgwzwgk/2024zcwj/", ".htm"),
    ("北京经济技术开发区管委会", "通知公告", "https://kfqgw.beijing.gov.cn/zwgkkfq/tzgg/", ".html"),
    ("北京经济技术开发区管委会", "政策文件", "https://kfqgw.beijing.gov.cn/zwgkkfq/2024zcwj/", ".html"),
]

TONGZHOU_SUBS = ["tzyw", "bmdt", "jxzdt", "fzxxw", "bjcsfzxb"]
DECLARE_ORG = "北京市政策兑现专区"


def _mk(it, org, column):
    return {"t": it["title"], "u": it["url"], "o": org, "c": column,
            "d": it.get("publish_date") or None, "_kind": "article"}


_WARNED_URLS = set()  # 同一未解析链接每轮只告警一次, 免被多页重复链接刷屏


def _fallback_items(html, page_url, known_urls, org, column):
    """兜底补漏: 用宽松口径全量抽取列表页链接, 与主解析结果对比——
    主解析漏掉的 (标题达标) 当场补入; 标题不可用的记日志告警, 不静默丢弃。
    known_urls: 本页已解析 + 此前已见的 url 集合 (不修改, 返回补入条目列表)"""
    extra = []
    for lk in base.extract_links_loose(html, page_url):
        if lk["url"] in known_urls:
            continue
        if len(lk["title"] or "") >= 8:
            extra.append(_mk(lk, org, column))
        elif lk["url"] not in _WARNED_URLS:
            _WARNED_URLS.add(lk["url"])
            base.log("兜底告警 %s-%s: 列表页有链接未被主解析收录且标题不可用: %s"
                     % (org, column, lk["url"]))
    if extra:
        base.log("兜底补漏 %s-%s: +%d 条 (主解析遗漏)" % (org, column, len(extra)))
    return extra


def scrape_column_deep(org, column, base_url, ext):
    """翻页深抓一个栏目: 首页 + index_1..index_(MAX_PAGES-1), 某页无新增即停"""
    out, seen = [], set()
    for n in range(MAX_PAGES):
        url = base_url if n == 0 else "%sindex_%d%s" % (base_url, n, ext)
        try:
            html = base.http_get(url, timeout=25)
        except Exception:
            if n == 0:
                raise
            break
        if "404 Not Found" in html[:600] and n > 0:
            break
        items = base.parse_list_page(html, base_url, limit=PAGE_LIMIT)
        new = [it for it in items if it["url"] not in seen]
        for it in new:
            seen.add(it["url"])
        out.extend(_mk(it, org, column) for it in new)
        fb = _fallback_items(html, url, seen, org, column)
        for e in fb:
            seen.add(e["u"])
        out.extend(fb)
        if not new and not fb:
            break
        time.sleep(0.2)
    return out


def scrape_tongzhou():
    """通州要闻动态: 聚合页 + 子频道首页 (各约15条), 合并去重"""
    org, column = "通州区人民政府", "要闻动态"
    urls = ["https://www.bjtzh.gov.cn/bjtz/ywdt/index.shtml"] + [
        "https://www.bjtzh.gov.cn/bjtz/ywdt/%s/index.shtml" % s for s in TONGZHOU_SUBS]
    out, seen = [], set()
    last_err = None
    for u in urls:
        try:
            html = base.http_get(u, timeout=25)
            for it in base.parse_list_page(html, u, limit=PAGE_LIMIT):
                if it["url"] in seen:
                    continue
                seen.add(it["url"])
                out.append(_mk(it, org, column))
            for e in _fallback_items(html, u, seen, org, column):
                seen.add(e["u"])
                out.append(e)
        except Exception as e:
            last_err = e
        time.sleep(0.2)
    if not out and last_err:
        raise last_err
    return out


def _declare_raw(page_size=100):
    """兑现专区全量分页 (pageSize=100, 约30页): 复用 sources 的字段口径"""
    base_url = "https://zhengce.beijing.gov.cn/lron/v2/bones/th/search/unifySearch"
    headers = {"server-origin": "https://zhengce.beijing.gov.cn",
               "Referer": "https://zhengce.beijing.gov.cn/"}
    items, page, total = [], 1, None
    while True:
        q = urllib.parse.urlencode({"type": "themeType", "page": page, "pageSize": page_size})
        raw = base.http_get(base_url + "?" + q, headers=headers, allow_partial=False)
        d = json.loads(raw)
        if d.get("code") != 0:
            raise RuntimeError("兑现专区接口返回异常: %s" % d.get("message"))
        data = d["data"]
        if total is None:
            total = data["pagination"]["total"]
        for it in data["list"]:
            tid = it.get("id")
            title = (it.get("title") or "").strip()
            if not tid or not title:
                continue
            start_s = it.get("startDateDeclare") or base.parse_date(it.get("startDate"))
            end_s = it.get("endDateDeclare") or base.parse_date(it.get("endDate"))
            money = ""
            if it.get("moneyMin") is not None or it.get("moneyMax") is not None:
                money = "%s-%s 万元" % (it.get("moneyMin"), it.get("moneyMax"))
            items.append({
                "title": title,
                "url": "https://zhengce.beijing.gov.cn/#/themeTypeDetail?themeTypeId=%s" % tid,
                "region": base.REGION_MAP.get(str(it.get("region") or ""), ""),
                "gov": it.get("govName") or "",
                "start": start_s, "end": end_s, "money": money,
            })
        if page * page_size >= (total or 0):
            break
        page += 1
        time.sleep(0.2)
    return items


def scrape_declare_v3():
    """兑现专区全量, 字段改分享版口径: rg/gv/st/de/mm"""
    raw = _declare_raw()
    out = []
    for it in raw:
        d = {"t": it["title"], "u": it["url"], "o": DECLARE_ORG, "c": "项目申报",
             "_kind": "declare"}
        if it.get("region"):
            d["rg"] = it["region"]
        if it.get("gov"):
            d["gv"] = it["gov"]
        # 状态按抓取当日 (CST) 重算, 不用 sources 模块导入时冻结的日期
        import datetime as _dt
        _today = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).date().isoformat()
        if it.get("end") and _today > it["end"]:
            _st = "已截止"
        elif it.get("start") and _today < it["start"]:
            _st = "未开始"
        else:
            _st = "申报中"
        d["st"] = _st
        if it.get("end"):
            d["de"] = it["end"]
        # money 形如 "None-50 万元" / "10-100 万元": 取上限为 mm (最高支持额度, 万)
        m = re.match(r"^\s*([\d.]+|None)\s*-\s*([\d.]+|None)\s*万元", it.get("money") or "")
        if m and m.group(2) != "None":
            try:
                v = float(m.group(2))
                d["mm"] = int(v) if v == int(v) else v
            except Exception:
                pass
        out.append(d)
    return out


def scrape_miit_lib():
    raw = base.scrape_miit_search(max_items=300)
    out = []
    for it in raw:
        d = {"t": it["title"], "u": it["url"], "o": "工业和信息化部", "c": "政策文件库",
             "d": it.get("publish_date") or None, "_kind": "article"}
        if it.get("doc_no"):
            d["fn"] = it["doc_no"]
        out.append(d)
    return out


def scrape_wjgs():
    """工信部文件公示: 列表页为壳时返回空 (主流程记为 0, 不拖垮其他来源)"""
    url = "https://www.miit.gov.cn/zwgk/wjgs/index.html"
    html = base.http_get(url, timeout=25)
    items = base.parse_list_page(html, url, limit=PAGE_LIMIT)
    out = [_mk(it, "工业和信息化部", "文件公示") for it in items]
    out.extend(_fallback_items(html, url, {it["url"] for it in items},
                               "工业和信息化部", "文件公示"))
    return out


def scrape_zjtx_v3():
    """梯度培育平台部级公告: POST tzggPagination, 响应 {data:{pageCount,pageContent}}"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj), urllib.request.HTTPSHandler(context=ctx))
    warm = urllib.request.Request("https://zjtx.miit.gov.cn/zxqySy/tzggMore",
                                  headers={"User-Agent": base.UA})
    with opener.open(warm, timeout=30) as r:
        r.read()
    headers = {"User-Agent": base.UA, "Referer": "https://zjtx.miit.gov.cn/zxqySy/tzggMore",
               "X-Requested-With": "XMLHttpRequest"}
    out, page, page_count = [], 1, 1
    while page <= page_count:
        body = urllib.parse.urlencode(
            {"page": page, "pageSize": 20, "title": "", "lx": ""}).encode("utf-8")
        req = urllib.request.Request("https://zjtx.miit.gov.cn/zxqySy/tzggPagination",
                                     data=body, headers=headers, method="POST")
        with opener.open(req, timeout=30) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        data = d.get("data") or {}
        page_count = int(data.get("pageCount") or 1)
        rows = data.get("pageContent") or []
        if not rows:
            break
        for it in rows:
            title = (it.get("title") or "").strip()
            rid = it.get("id")
            if not title or rid is None:
                continue
            out.append({
                "t": title,
                "u": "https://zjtx.miit.gov.cn/zxqySy/tzggView?id=%s" % rid,
                "o": "优质中小企业梯度培育平台", "c": "部级公告",
                "d": base.parse_date(it.get("fbsj") or ""),
                "_kind": "article",
            })
        page += 1
        time.sleep(0.3)
    if not out:
        raise RuntimeError("梯度培育平台接口未返回条目")
    return out


# 监测来源清单 (与分享版 sources 口径一致): (机构, 栏目, 栏目首页)
SOURCE_DEFS = [
    ("北京市经济和信息化局", "通知公告", "https://jxj.beijing.gov.cn/jxdt/tzgg/"),
    ("北京市经济和信息化局", "政策文件", "https://jxj.beijing.gov.cn/zwgk/2024zcwj/"),
    ("北京市经济和信息化局", "办事通知", "https://jxj.beijing.gov.cn/bsfw/bstz/"),
    ("北京市科学技术委员会", "通知公告", "https://kw.beijing.gov.cn/zwgk/tzgg/"),
    ("北京市科学技术委员会", "政策文件", "https://kw.beijing.gov.cn/zwgk/zcwj/"),
    ("北京市发展和改革委员会", "通知通告", "https://fgw.beijing.gov.cn/gzdt/tztg/"),
    ("北京市发展和改革委员会", "政策文件", "https://fgw.beijing.gov.cn/fgwzwgk/2024zcwj/"),
    ("北京经济技术开发区管委会", "通知公告", "https://kfqgw.beijing.gov.cn/zwgkkfq/tzgg/"),
    ("北京经济技术开发区管委会", "政策文件", "https://kfqgw.beijing.gov.cn/zwgkkfq/2024zcwj/"),
    ("优质中小企业梯度培育平台", "部级公告", "https://zjtx.miit.gov.cn/zxqySy/tzggMore"),
    ("通州区人民政府", "要闻动态", "https://www.bjtzh.gov.cn/bjtz/ywdt/index.shtml"),
    (DECLARE_ORG, "项目申报", "https://zhengce.beijing.gov.cn/#/declare"),
    ("工业和信息化部", "政策文件库", "https://wap.miit.gov.cn/search/zcwjk.html"),
    ("工业和信息化部", "文件公示", "https://www.miit.gov.cn/zwgk/wjgs/index.html"),
]


def collect_all():
    """抓全部来源; 返回 (items, report)
    report: [{"o":机构,"c":栏目,"n":条数,"ok":bool}] 与 SOURCE_DEFS 同序"""
    jobs = {}
    for org, column, _u in SOURCE_DEFS:
        jobs[(org, column)] = None
    runners = []
    for org, column, _url, ext in DEEP_SOURCES:
        runners.append((org, column,
                        (lambda o, c, u, e: lambda: scrape_column_deep(o, c, u, e))(org, column, _url, ext)))
    runners.append(("通州区人民政府", "要闻动态", scrape_tongzhou))
    runners.append(("优质中小企业梯度培育平台", "部级公告", scrape_zjtx_v3))
    runners.append((DECLARE_ORG, "项目申报", scrape_declare_v3))
    runners.append(("工业和信息化部", "政策文件库", scrape_miit_lib))
    runners.append(("工业和信息化部", "文件公示", scrape_wjgs))

    got = {}
    for org, column, fn in runners:
        label = "%s-%s" % (org, column)
        try:
            items = fn()
            got[(org, column)] = items
            base.log("抓取成功 %s: %d 条" % (label, len(items)))
        except Exception as e:
            got[(org, column)] = None
            base.log("抓取失败 %s: %s" % (label, str(e)[:160]))

    all_items, report = [], []
    for org, column, _u in SOURCE_DEFS:
        items = got.get((org, column))
        if items is None:
            report.append({"o": org, "c": column, "n": 0, "ok": False})
        else:
            report.append({"o": org, "c": column, "n": len(items), "ok": True})
            all_items.extend(items)
    # 全局按链接去重
    dedup, seen = [], set()
    for it in all_items:
        if it["u"] in seen:
            continue
        seen.add(it["u"])
        dedup.append(it)
    base.log("合计去重后 %d 条" % len(dedup))
    return dedup, report
