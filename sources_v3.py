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

# 全站监测栏目表 (2026-11-11 按老大要求由"只盯几个栏目"改为全站监测):
# 六站每个会发带日期文章的栏目都纳入——两轮发现脚本实测 336+389 个候选逐个试抓后
# 留下的叶子栏目; 剔除: 首页/一级聚合页 (与叶子重复)、专题专栏 (多为陈年微站)、
# 纯静态页 (机构职能/办事指南手册/价格目录/年报)。
# (机构, 栏目, 列表页, 分页后缀, 类别, 页深) 类别供页面分类 (fm) 用;
# 页深: 政策/解读/通知 6 页, 新闻/数据/互动 1-3 页 (通州分页形态未确认, 只取第一页)。
JXJ = "北京市经济和信息化局"
KW = "北京市科学技术委员会"
FGW = "北京市发展和改革委员会"
KFQ = "北京经济技术开发区管委会"
TZ = "通州区人民政府"
MIIT = "工业和信息化部"

FULL_SOURCES = [
    # 经信局
    (JXJ, "通知公告", "https://jxj.beijing.gov.cn/jxdt/tzgg/", ".html", "通知公告", 6),
    (JXJ, "政策文件", "https://jxj.beijing.gov.cn/zwgk/2024zcwj/", ".html", "政策文件", 6),
    (JXJ, "政策解读", "https://jxj.beijing.gov.cn/zwgk/2024zcjd/", ".html", "政策解读", 6),
    (JXJ, "办事通知", "https://jxj.beijing.gov.cn/bsfw/bstz/", ".html", "通知公告", 3),
    (JXJ, "采购公告", "https://jxj.beijing.gov.cn/zwgk/zfcggg/", ".html", "通知公告", 3),
    (JXJ, "人事信息", "https://jxj.beijing.gov.cn/zwgk/rsxx/", ".html", "通知公告", 3),
    (JXJ, "财政预决算", "https://jxj.beijing.gov.cn/zwgk/czyjs/", ".html", "通知公告", 2),
    (JXJ, "应急管理", "https://jxj.beijing.gov.cn/zwgk/jyjgl/", ".html", "通知公告", 2),
    (JXJ, "规划计划", "https://jxj.beijing.gov.cn/zwgk/ghjh/", ".html", "政策文件", 3),
    # 经信局页上的「中央精神/北京要闻」是跳转市政府门户的壳, 真栏目在 beijing.gov.cn, 归北京市人民政府
    ("北京市人民政府", "中央精神", "https://www.beijing.gov.cn/ywdt/dzyjs/", ".html", "新闻动态", 2),
    ("北京市人民政府", "北京要闻", "https://www.beijing.gov.cn/ywdt/yaowen/", ".html", "新闻动态", 2),
    (JXJ, "热点聚焦", "https://jxj.beijing.gov.cn/jxdt/zwyw/", ".html", "新闻动态", 3),
    (JXJ, "工作动态", "https://jxj.beijing.gov.cn/jxdt/gzdt/", ".html", "新闻动态", 3),
    (JXJ, "专精特新", "https://jxj.beijing.gov.cn/hyjj/zjtx/", ".html", "新闻动态", 3),
    (JXJ, "伙伴计划", "https://jxj.beijing.gov.cn/ztzl/ywzt/hbjh/", ".html", "新闻动态", 2),
    (JXJ, "经济运行", "https://jxj.beijing.gov.cn/jxsj/jjyx/", ".html", "数据发布", 3),
    (JXJ, "办事统计", "https://jxj.beijing.gov.cn/bsfw/bstj/", ".html", "数据发布", 2),
    (JXJ, "留言统计", "https://jxj.beijing.gov.cn/zmhd/gzlxtj/", ".html", "数据发布", 1),
    (JXJ, "意见征集", "https://jxj.beijing.gov.cn/zmhd/yjzj/", ".html", "互动征集", 3),
    (JXJ, "视频访谈", "https://jxj.beijing.gov.cn/zmhd/xwfb/", ".html", "互动征集", 2),
    # 科委
    (KW, "通知公告", "https://kw.beijing.gov.cn/zwgk/tzgg/", ".html", "通知公告", 6),
    (KW, "政策文件", "https://kw.beijing.gov.cn/zwgk/zcwj/", ".html", "政策文件", 6),
    (KW, "政策解读", "https://kw.beijing.gov.cn/zwgk/zcjd/", ".html", "政策解读", 6),
    (KW, "规划信息", "https://kw.beijing.gov.cn/zwgk/ghxx/", ".html", "政策文件", 3),
    (KW, "办事结果", "https://kw.beijing.gov.cn/zwfw/bsjg/", ".html", "通知公告", 3),
    (KW, "工作动态", "https://kw.beijing.gov.cn/xwdt/bmdt/", ".html", "新闻动态", 3),
    (KW, "媒体报道", "https://kw.beijing.gov.cn/xwdt/mtbd/", ".html", "新闻动态", 2),
    (KW, "热点转载", "https://kw.beijing.gov.cn/xwdt/rdzz/", ".html", "新闻动态", 2),
    (KW, "科创活动", "https://kw.beijing.gov.cn/xwdt/kcyx/kcyxkchd/", ".html", "新闻动态", 2),
    (KW, "产业发展", "https://kw.beijing.gov.cn/xwdt/kcyx/xwdtcyfz/", ".html", "新闻动态", 2),
    (KW, "开放合作", "https://kw.beijing.gov.cn/xwdt/kcyx/xwdtkfhz/", ".html", "新闻动态", 2),
    (KW, "科技前沿", "https://kw.beijing.gov.cn/xwdt/kcyx/xwdtkjqy/", ".html", "新闻动态", 2),
    (KW, "三城一区", "https://kw.beijing.gov.cn/xwdt/kcyx/xwdtscyqld/", ".html", "新闻动态", 2),
    (KW, "深化改革", "https://kw.beijing.gov.cn/xwdt/kcyx/xwdtshgg/", ".html", "新闻动态", 2),
    (KW, "园区企业", "https://kw.beijing.gov.cn/xwdt/kcyx/xwdtyqqy/", ".html", "新闻动态", 2),
    (KW, "科技成果转化", "https://kw.beijing.gov.cn/ztzl/kjcgzh/", ".html", "新闻动态", 2),
    (KW, "数据发布", "https://kw.beijing.gov.cn/zwgk/sjfb/", ".html", "数据发布", 2),
    (KW, "调查征集", "https://kw.beijing.gov.cn/zmhd/dczj/", ".html", "互动征集", 3),
    (KW, "调查反馈", "https://kw.beijing.gov.cn/zmhd/dcfk/", ".html", "互动征集", 2),
    # 发改委
    (FGW, "通知通告", "https://fgw.beijing.gov.cn/gzdt/tztg/", ".htm", "通知公告", 6),
    (FGW, "政策文件", "https://fgw.beijing.gov.cn/fgwzwgk/2024zcwj/", ".htm", "政策文件", 6),
    (FGW, "政策解读", "https://fgw.beijing.gov.cn/fgwzwgk/2024zcjd/", ".htm", "政策解读", 6),
    (FGW, "依申请转主动公开", "https://fgw.beijing.gov.cn/fgwzwgk/ysqzzdgk/", ".htm", "通知公告", 3),
    (FGW, "行政许可公示", "https://fgw.beijing.gov.cn/fzggzl/yfxz/", ".htm", "通知公告", 3),
    (FGW, "发改之声", "https://fgw.beijing.gov.cn/gzdt/fgzs/", ".htm", "新闻动态", 3),
    (FGW, "发展北京", "https://fgw.beijing.gov.cn/gzdt/fzbjgzh/", ".htm", "新闻动态", 2),
    (FGW, "区域合作", "https://fgw.beijing.gov.cn/fzggzl/zyhz/", ".htm", "新闻动态", 2),
    (FGW, "调查征集", "https://fgw.beijing.gov.cn/zmhd/dczj/", ".htm", "互动征集", 3),
    # 经开区
    (KFQ, "通知公告", "https://kfqgw.beijing.gov.cn/zwgkkfq/tzgg/", ".html", "通知公告", 6),
    (KFQ, "政策文件", "https://kfqgw.beijing.gov.cn/zwgkkfq/2024zcwj/", ".html", "政策文件", 6),
    (KFQ, "政策解读", "https://kfqgw.beijing.gov.cn/zwgkkfq/2024zcjd/", ".html", "政策解读", 6),
    (KFQ, "计划规划", "https://kfqgw.beijing.gov.cn/zwgkkfq/ghjh/", ".html", "政策文件", 2),
    (KFQ, "头条", "https://kfqgw.beijing.gov.cn/ywdt/tt/", ".html", "新闻动态", 2),
    (KFQ, "工作动态", "https://kfqgw.beijing.gov.cn/ywdt/gzdt/", ".html", "新闻动态", 3),
    (KFQ, "科技成果转化高地", "https://kfqgw.beijing.gov.cn/ywdt/kjcgzhgd/", ".html", "新闻动态", 2),
    (KFQ, "高端产业发展高地", "https://kfqgw.beijing.gov.cn/ywdt/gdcyfzgd/", ".html", "新闻动态", 2),
    (KFQ, "对外开放高地", "https://kfqgw.beijing.gov.cn/ywdt/dwkfgd/", ".html", "新闻动态", 2),
    (KFQ, "高水平人才高地", "https://kfqgw.beijing.gov.cn/ywdt/gsprcgd/", ".html", "新闻动态", 2),
    (KFQ, "创新展示", "https://kfqgw.beijing.gov.cn/cxyzkfq/yzal/", ".html", "新闻动态", 2),
    (KFQ, "创新举措", "https://kfqgw.beijing.gov.cn/cxyzkfq/cxcg/", ".html", "新闻动态", 2),
    (KFQ, "数据发布", "https://kfqgw.beijing.gov.cn/zwgkkfq/sj/", ".html", "数据发布", 2),
    (KFQ, "办事统计", "https://kfqgw.beijing.gov.cn/zwfwkfq/bstj/", ".html", "数据发布", 2),
    (KFQ, "意见征集", "https://kfqgw.beijing.gov.cn/zmhdkfq/yjzj/", ".html", "互动征集", 3),
    (KFQ, "常见问题", "https://kfqgw.beijing.gov.cn/zmhdkfq/cjwt/", ".html", "互动征集", 2),
    (KFQ, "在线访谈", "https://kfqgw.beijing.gov.cn/zmhdkfq/zxft/", ".html", "互动征集", 2),
    # 通州 (分页形态未确认, 只取第一页)
    (TZ, "通知公告", "https://www.bjtzh.gov.cn/bjtz/zwgk/tzgg/", ".shtml", "通知公告", 1),
    (TZ, "政策文件", "https://www.bjtzh.gov.cn/bjtz/zwgk/zcwj/", ".shtml", "政策文件", 1),
    (TZ, "政策解读", "https://www.bjtzh.gov.cn/bjtz/zwgk/zcjd/", ".shtml", "政策解读", 1),
    (TZ, "通州动态", "https://www.bjtzh.gov.cn/bjtz/ywdt/tzyw/", ".shtml", "新闻动态", 1),
    (TZ, "部门动态", "https://www.bjtzh.gov.cn/bjtz/ywdt/bmdt/", ".shtml", "新闻动态", 1),
    (TZ, "街乡镇动态", "https://www.bjtzh.gov.cn/bjtz/ywdt/jxzdt/", ".shtml", "新闻动态", 1),
    (TZ, "副中心新闻", "https://www.bjtzh.gov.cn/bjtz/ywdt/fzxxw/", ".shtml", "新闻动态", 1),
    (TZ, "热点关注", "https://www.bjtzh.gov.cn/bjtz/ywdt/rdgz/", ".shtml", "新闻动态", 1),
    (TZ, "会议信息", "https://www.bjtzh.gov.cn/bjtz/zwgk/hyxx/", ".shtml", "新闻动态", 1),
    (TZ, "回应关切", "https://www.bjtzh.gov.cn/bjtz/zwgk/hygq/", ".shtml", "新闻动态", 1),
    (TZ, "冬季供暖", "https://www.bjtzh.gov.cn/bjtz/ztzl/2026-2027nddjgn/", ".shtml", "通知公告", 1),
    (TZ, "政务服务中心", "https://www.bjtzh.gov.cn/bjtz/ztzl/csfzxzwfwzx/", ".shtml", "通知公告", 1),
    (TZ, "开放数据", "https://www.bjtzh.gov.cn/bjtz/zwgk/kfsj/", ".shtml", "数据发布", 1),
    (TZ, "办事服务数据", "https://www.bjtzh.gov.cn/bjtz/zwfw/bsfwsj71/", ".shtml", "数据发布", 1),
    (TZ, "留言统计", "https://www.bjtzh.gov.cn/bjtz/zwfw/lytjsjwz/", ".shtml", "数据发布", 1),
    (TZ, "意见征集", "https://www.bjtzh.gov.cn/bjtz/zmhd/zcxwjyjzj/", ".shtml", "互动征集", 1),
    (TZ, "政务直播", "https://www.bjtzh.gov.cn/bjtz/zmhd/fzxzwfwyzb/", ".shtml", "互动征集", 1),
    # 工信部
    (MIIT, "新闻发布", "https://www.miit.gov.cn/xwfb/xwfbh/", ".html", "新闻动态", 2),
]
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


def scrape_column_deep(org, column, base_url, ext, max_pages=None):
    """翻页深抓一个栏目: 首页 + index_1..index_(页深-1), 某页无新增即停"""
    out, seen = [], set()
    for n in range(MAX_PAGES if max_pages is None else max_pages):
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
    """工信部政策文件库: 其搜索接口偶发长时间无响应 (2026-11-11 实测挂起 10 分钟+),
    故给整源加 150 秒硬截止: 超时按失败处理、库内旧条目保留, 不拖住整轮抓取"""
    import threading
    box = {}

    def work():
        try:
            box["raw"] = base.scrape_miit_search(max_items=300)
        except Exception as e:  # noqa: BLE001
            box["err"] = e

    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(150)
    if th.is_alive():
        raise RuntimeError("工信部政策文件库接口 150 秒未响应, 本轮跳过")
    if "err" in box:
        raise box["err"]
    raw = box.get("raw") or []
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


# 监测来源清单: (机构, 栏目, 栏目首页) —— 全站栏目 + 4 个接口型特殊源
SOURCE_DEFS = [(o, n, u) for (o, n, u, _e, _f, _p) in FULL_SOURCES] + [
    ("优质中小企业梯度培育平台", "部级公告", "https://zjtx.miit.gov.cn/zxqySy/tzggMore"),
    (DECLARE_ORG, "项目申报", "https://zhengce.beijing.gov.cn/#/declare"),
    ("工业和信息化部", "政策文件库", "https://wap.miit.gov.cn/search/zcwjk.html"),
    ("工业和信息化部", "文件公示", "https://www.miit.gov.cn/zwgk/wjgs/index.html"),
]


def collect_all():
    """抓全部来源 (全站监测, 8 线程并发); 返回 (items, report)
    report: [{"o":机构,"c":栏目,"n":条数,"ok":bool}] 与 SOURCE_DEFS 同序;
    合并去重按 SOURCE_DEFS 顺序, 同一 URL 以靠前栏目归属为准 (归属稳定)"""
    from concurrent.futures import ThreadPoolExecutor
    runners = []
    for org, column, url, ext, fam, pages in FULL_SOURCES:
        runners.append((org, column,
                        (lambda o, c, u, e, p: lambda: scrape_column_deep(
                            o, c, u, e, max_pages=p))(org, column, url, ext, pages)))
    runners.append(("优质中小企业梯度培育平台", "部级公告", scrape_zjtx_v3))
    runners.append((DECLARE_ORG, "项目申报", scrape_declare_v3))
    runners.append(("工业和信息化部", "政策文件库", scrape_miit_lib))
    runners.append(("工业和信息化部", "文件公示", scrape_wjgs))

    def run(rc):
        org, column, fn = rc
        try:
            items = fn()
            base.log("抓取成功 %s-%s: %d 条" % (org, column, len(items)))
            return (org, column, items)
        except Exception as e:
            base.log("抓取失败 %s-%s: %s" % (org, column, str(e)[:160]))
            return (org, column, None)

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(run, runners))
    got = {(o, c): items for o, c, items in results}
    all_items, report = [], []
    for org, column, _u in SOURCE_DEFS:
        items = got.get((org, column))
        if items is None:
            report.append({"o": org, "c": column, "n": 0, "ok": False})
        else:
            report.append({"o": org, "c": column, "n": len(items), "ok": True})
            all_items.extend(items)
    dedup, seen = [], set()
    for it in all_items:
        if it["u"] in seen:
            continue
        seen.add(it["u"])
        dedup.append(it)
    base.log("合计去重后 %d 条" % len(dedup))
    return dedup, report
