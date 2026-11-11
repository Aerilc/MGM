# -*- coding: utf-8 -*-
"""
政策监测数据源抓取模块 (与线上监测站同一批 14 个来源、同一套口径)
仅使用 Python 标准库。供 mcp_server.py 调用: collect_all() -> (items, report)
item 字段: org / column / title / url / publish_date / declare_status /
           region / gov / start / end / amount / doc_no
本模块由 feishu-sync 的 sync.py 抓取部分抽取而来 (已实测与正式站条数吻合)。
"""
import json
import os
import re
import ssl
import sys
import time
import datetime
import urllib.request
import urllib.parse
import urllib.error
from html import unescape as html_unescape

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
LOG_DIR = os.path.join(BASE_DIR, "logs")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TODAY = datetime.date.today()
TODAY_S = TODAY.isoformat()

LOG_LINES = []


def log(msg):
    line = "[%s] %s" % (datetime.datetime.now().strftime("%H:%M:%S"), msg)
    LOG_LINES.append(line)
    try:
        print(line)
    except Exception:
        pass


def flush_log():
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, "sync-%s.log" % TODAY_S)
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(LOG_LINES) + "\n")
    except Exception:
        pass


# ---------------- 通用 HTTP ----------------

def http_get(url, headers=None, timeout=30, retries=3, allow_partial=True):
    import http.client
    last = None
    for i in range(retries + 1):
        try:
            h = {"User-Agent": UA, "Accept-Encoding": "identity", "Connection": "close"}
            if headers:
                h.update(headers)
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                enc = r.headers.get_content_charset()
                try:
                    raw = r.read()
                except http.client.IncompleteRead as e:
                    # 连接中途断开: HTML 栏目页可用已收到的部分; JSON 接口必须整包, 继续重试
                    if not allow_partial or len(e.partial) < 3000:
                        raise
                    raw = e.partial
            for enc_try in ([enc] if enc else []) + ["utf-8", "gb18030"]:
                try:
                    return raw.decode(enc_try)
                except Exception:
                    continue
            return raw.decode("utf-8", "replace")
        except Exception as e:
            last = e
            if i < retries:
                time.sleep(2)
    # urllib 多次失败时用系统自带 curl 兜底 (Windows 10/11 自带 curl.exe)
    text = _curl_get(url, headers, timeout)
    if text is not None:
        return text
    raise last


def _curl_get(url, headers=None, timeout=30):
    import shutil
    import subprocess
    exe = shutil.which("curl") or shutil.which("curl.exe")
    if not exe:
        return None
    cmd = [exe, "-sL", "--max-time", str(timeout), "-A", UA,
           "-H", "Accept-Encoding: identity", "-H", "Connection: close"]
    if headers:
        for k, v in headers.items():
            cmd += ["-H", "%s: %s" % (k, v)]
    cmd.append(url)
    for _ in range(2):
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
            raw = p.stdout
            if p.returncode == 0 and len(raw) > 0:
                for enc_try in ("utf-8", "gb18030"):
                    try:
                        return raw.decode(enc_try)
                    except Exception:
                        continue
                return raw.decode("utf-8", "replace")
        except Exception:
            pass
        time.sleep(2)
    return None


def http_post_json(url, body, headers=None, timeout=30):
    h = {"User-Agent": UA, "Content-Type": "application/json; charset=utf-8"}
    if headers:
        h.update(headers)
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def http_post_form(url, params, headers=None, timeout=30):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def date_to_ms(d):
    """'2026-11-04' -> 毫秒时间戳 (飞书日期字段格式); 非法返回 None"""
    if not d:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(d))
    if not m:
        return None
    try:
        dt = datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def parse_date(s):
    """从各种文本里抽出 yyyy-MM-dd, 抽不到返回 None"""
    if not s:
        return None
    m = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", str(s))
    if m:
        return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"(20\d{2})(\d{2})(\d{2})", str(s))
    if m:
        return "%s-%s-%s" % (m.group(1), m.group(2), m.group(3))
    return None


# ---------------- 抓取: 通用栏目页解析 ----------------

def clean_text(s):
    s = re.sub(r"<[^>]+>", "", s or "")
    s = html_unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def parse_list_page(html, page_url, limit=25):
    """从政府网站栏目列表页抽文章: 返回 [{title,url,publish_date}] 按页面顺序"""
    out, seen = [], set()
    for m in re.finditer(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", html, re.S | re.I):
        href, inner = m.group(1).strip(), m.group(2)
        if not href or href.startswith(("javascript:", "#", "mailto:")):
            continue
        url = urllib.parse.urljoin(page_url, href)
        if url.rstrip("/") == page_url.rstrip("/"):
            continue
        path = urllib.parse.urlparse(url).path
        # 文章页特征: 路径里带日期或 art 段, 且以 .html/.shtml/.htm 结尾
        if not re.search(r"\.(s?html?|htm)$", path):
            continue
        if not re.search(r"(20\d{6}|/art/|/20\d{2}/|t20\d{6})", path):
            continue
        title_m = re.search(r"title=[\"']([^\"']+)[\"']", m.group(0))
        title = clean_text(title_m.group(1)) if title_m else clean_text(inner)
        if len(title) < 8:
            continue
        if url in seen:
            continue
        seen.add(url)
        # 日期: 先在链接前后 400 字里找, 再从 URL 里抽
        window = html[max(0, m.start() - 200): m.end() + 400]
        pub = parse_date(window) or parse_date(url)
        out.append({"title": title, "url": url, "publish_date": pub})
        if len(out) >= limit:
            break
    return out


HTML_SOURCES = [
    # (机构, 栏目, 列表页)
    ("北京市经济和信息化局", "通知公告", "https://jxj.beijing.gov.cn/jxdt/tzgg/"),
    ("北京市经济和信息化局", "政策文件", "https://jxj.beijing.gov.cn/zwgk/2024zcwj/"),
    ("北京市经济和信息化局", "办事通知", "https://jxj.beijing.gov.cn/bsfw/bstz/"),
    ("北京市科学技术委员会", "通知公告", "https://kw.beijing.gov.cn/zwgk/tzgg/"),
    ("北京市科学技术委员会", "政策文件", "https://kw.beijing.gov.cn/zwgk/zcwj/"),
    ("北京市发展和改革委员会", "通知通告", "https://fgw.beijing.gov.cn/gzdt/tztg/"),
    ("北京市发展和改革委员会", "政策文件", "https://fgw.beijing.gov.cn/fgwzwgk/2024zcwj/"),
    ("北京经济技术开发区管理委员会", "通知公告", "https://kfqgw.beijing.gov.cn/zwgkkfq/tzgg/"),
    ("北京经济技术开发区管理委员会", "政策文件", "https://kfqgw.beijing.gov.cn/zwgkkfq/2024zcwj/"),
    ("通州区人民政府", "要闻动态", "https://www.bjtzh.gov.cn/bjtz/ywdt/index.shtml"),
    # 工信部文件公示: 官网列表页本身是壳, 抓不到时自动跳过 (该来源量极少)
    ("工业和信息化部", "文件公示", "https://www.miit.gov.cn/zwgk/wjgs/index.html"),
]


def scrape_html_source(org, column, page_url):
    html = http_get(page_url)
    items = parse_list_page(html, page_url)
    return [dict(it, org=org, column=column, kind="article") for it in items]


# ---------------- 抓取: 政策兑现专区 (全量) ----------------

REGION_MAP = {
    "110101": "东城区", "110102": "西城区", "110105": "朝阳区", "110106": "丰台区",
    "110107": "石景山区", "110108": "海淀区", "110109": "门头沟区", "110111": "房山区",
    "110112": "通州区", "110113": "顺义区", "110114": "昌平区", "110115": "大兴区",
    "110116": "怀柔区", "110117": "平谷区", "110118": "密云区", "110119": "延庆区",
    "110120": "北京经济技术开发区", "110000": "北京市", "11": "北京市",
}


def declare_status(start_s, end_s):
    if end_s and TODAY_S > end_s:
        return "已截止"
    if start_s and TODAY_S < start_s:
        return "未开始"
    return "申报中"


def scrape_declare(max_items=None):
    """兑现专区 unifySearch 接口: GET 分页全量, type=themeType"""
    base = "https://zhengce.beijing.gov.cn/lron/v2/bones/th/search/unifySearch"
    headers = {"server-origin": "https://zhengce.beijing.gov.cn",
               "Referer": "https://zhengce.beijing.gov.cn/"}
    items, page, page_size = [], 1, 20
    total = None
    while True:
        q = urllib.parse.urlencode({"type": "themeType", "page": page, "pageSize": page_size})
        raw = http_get(base + "?" + q, headers=headers, allow_partial=False)
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
            url = "https://zhengce.beijing.gov.cn/#/themeTypeDetail?themeTypeId=%s" % tid
            start_s = it.get("startDateDeclare") or parse_date(it.get("startDate"))
            end_s = it.get("endDateDeclare") or parse_date(it.get("endDate"))
            money = ""
            if it.get("moneyMin") is not None or it.get("moneyMax") is not None:
                money = "%s-%s 万元" % (it.get("moneyMin"), it.get("moneyMax"))
            # publishDate 在该系统里等于申报截止日, 不是真实发布日期; 未来的直接置空
            pub = it.get("publishDate")
            if pub and (pub == end_s or pub > TODAY_S):
                pub = None
            items.append({
                "title": title, "url": url, "org": "北京市人民政府", "column": "政策兑现专区",
                "kind": "declare", "publish_date": pub,
                "region": REGION_MAP.get(str(it.get("region") or ""), ""),
                "gov": it.get("govName") or "",
                "start": start_s, "end": end_s, "money": money,
                "status": declare_status(start_s, end_s),
            })
            if max_items and len(items) >= max_items:
                return items
        if page * page_size >= (total or 0):
            break
        page += 1
        time.sleep(0.3)
    return items


# ---------------- 抓取: 工信部政策文件库 (站群搜索接口) ----------------

def scrape_miit_search(max_items=300):
    base = "https://wap.miit.gov.cn/search-front-server/api/search/info"
    items, page, page_size = [], 1, 100
    while len(items) < max_items:
        q = urllib.parse.urlencode({"websiteid": "110000000000000", "cateid": 196,
                                    "page": page, "pagesize": page_size})
        raw = http_get(base + "?" + q, headers={"Referer": "https://wap.miit.gov.cn/search/zcwjk.html"},
                       allow_partial=False)
        d = json.loads(raw)
        results = d["data"]["searchResult"]["dataResults"]
        if not results:
            break
        for r in results:
            dd = r.get("data") or {}
            title = (dd.get("title") or "").strip()
            rel = dd.get("url") or ""
            if not title or not rel:
                continue
            url = rel if rel.startswith("http") else "https://www.miit.gov.cn" + rel
            items.append({
                "title": title, "url": url, "org": "工业和信息化部", "column": "政策文件库",
                "kind": "article", "publish_date": dd.get("jsearch_date") or parse_date(rel),
                "doc_no": dd.get("filenumbername") or "",
            })
            if len(items) >= max_items:
                break
        page += 1
        time.sleep(0.3)
    return items


# ---------------- 抓取: 优质中小企业梯度培育平台 (部级公告, 尽力) ----------------

def scrape_zjtx():
    """该站分页接口参数较特殊: 先预热会话再按其分页插件的约定 POST; 失败抛错由主流程记录"""
    import http.cookiejar
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj), urllib.request.HTTPSHandler(context=ctx))
    warm = urllib.request.Request("https://zjtx.miit.gov.cn/zxqySy/tzggMore",
                                  headers={"User-Agent": UA})
    with opener.open(warm, timeout=30) as r:
        r.read()
    headers = {"User-Agent": UA, "Referer": "https://zjtx.miit.gov.cn/zxqySy/tzggMore",
               "X-Requested-With": "XMLHttpRequest"}
    # 该站分页参数约定未完全公开: 依次尝试几组常见组合, 第一组返回 JSON 的为准
    raw = None
    last_err = None
    for params in ({"pageNo": 1, "pageSize": 20, "title": "", "lx": ""},
                   {"page": 1, "pageSize": 20, "title": "", "lx": ""},
                   {"currPage": 1, "pageSize": 20, "title": "", "lx": ""},
                   {"pageIndex": 1, "pageSize": 20, "title": "", "lx": ""},
                   {"pageNum": 1, "pageSize": 20, "title": "", "lx": ""}):
        try:
            body = urllib.parse.urlencode(params).encode("utf-8")
            req = urllib.request.Request("https://zjtx.miit.gov.cn/zxqySy/tzggPagination",
                                         data=body, headers=headers, method="POST")
            with opener.open(req, timeout=30) as r:
                cand = r.read().decode("utf-8", "replace")
            json.loads(cand)  # 能解析成 JSON 才算命中
            raw = cand
            break
        except Exception as e:
            last_err = e
    if raw is None:
        raise RuntimeError("梯度培育平台分页接口未命中参数组合: %s" % str(last_err)[:100])
    d = json.loads(raw)
    rows = d.get("pageContent") or d.get("list") or []
    items = []
    for it in rows:
        title = (it.get("title") or it.get("bt") or "").strip()
        rid = it.get("id")
        if not title or rid is None:
            continue
        items.append({
            "title": title,
            "url": "https://zjtx.miit.gov.cn/zxqySy/tzggView?id=%s" % rid,
            "org": "工业和信息化部", "column": "优质中小企业梯度培育平台",
            "kind": "article",
            "publish_date": parse_date(it.get("createTime") or it.get("fbsj") or it.get("publishDate") or ""),
        })
    if not items:
        raise RuntimeError("梯度培育平台接口未返回条目 (返回片段: %s)" % raw[:120])
    return items


def collect_all():
    """抓全部来源; 返回 (items, report_lines) —— 单个来源失败不影响其他来源"""
    all_items, report = [], []

    def run(label, fn):
        try:
            items = fn()
            all_items.extend(items)
            report.append("%s: %d 条" % (label, len(items)))
            log("抓取成功 %s: %d 条" % (label, len(items)))
            return items
        except Exception as e:
            report.append("%s: 失败 (%s)" % (label, str(e)[:100]))
            log("抓取失败 %s: %s" % (label, str(e)[:160]))
            return []

    for org, column, url in HTML_SOURCES:
        run("%s-%s" % (org, column), lambda o=org, c=column, u=url: scrape_html_source(o, c, u))
    run("政策兑现专区", scrape_declare)
    run("工信部-政策文件库", scrape_miit_search)
    run("梯度培育平台-部级公告", scrape_zjtx)
    # 按链接去重 (同一篇文章可能同时挂在两个栏目下)
    dedup, seen = [], set()
    for it in all_items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        dedup.append(it)
    log("合计去重后 %d 条" % len(dedup))
    return dedup, report
