# -*- coding: utf-8 -*-
"""a_stock_selection 共享库。

仅使用 Python 标准库，保证在 trae / hermes / workbuddy / qwenwork 等环境直接运行。
职责：数据获取（腾讯/新浪/东财直连）、公式与指标计算、配置与状态读写、文本输出。
数据源：仅腾讯/新浪/东财直连；接口不可用时显式记录降级（不静默、不缓存）。
公式依据：docs/定盘实时任务_公式清单.md（只读，不改动）。
"""
import os
import re
import sys
import json
import copy
import urllib.request
import urllib.parse
from datetime import datetime

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = os.path.join(SKILL_ROOT, "assets")
SAMPLE_DIR = os.path.join(ASSETS_DIR, "sample")

# ---------------------------------------------------------------------------
# 交易日历基线 —— 2026 年，来源：上交所《2026 年休市安排》官方公告。
# 仅收录"落在工作日"的休市日（共 19 天）；周末由 weekday() 判断覆盖，无需列出。
# 实际使用以 config.json 的 calendar 为准；此基线用于开箱即用与缺失兜底。
# ---------------------------------------------------------------------------
CALENDAR_BASELINE = {
    "holidays": [
        "2026-01-01", "2026-01-02",
        "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-23",
        "2026-04-06",
        "2026-05-01", "2026-05-04", "2026-05-05",
        "2026-06-19",
        "2026-09-25",
        "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",
    ],
    "coverage_to": "2026-12-31",
    "generated_at": "2026-10-08",
    "source": "上交所 2026 年休市安排(sse.com.cn)",
    "lead_days": 30,
}

# ---------------------------------------------------------------------------
# 阈值默认值 —— 全部来自 docs/定盘实时任务_公式清单.md
# ---------------------------------------------------------------------------
DEFAULT_THRESHOLDS = {
    "light": {
        "red_line_factor": 0.98,
        "cap": {"green": 1.0, "yellow": 0.5, "red": 0.2},
        "single": {"green": 0.40, "yellow": 0.20, "red": 0.0},
        "ma_periods": [5, 10, 20],
    },
    "mainline": {"topn": 5, "pct_min": 0.8, "amount_yi": 150.0,
                 "score_star": 4, "score_watch": 2, "days": 3},
    "leader": {"zt60_min": 3},
    "trend": {"zt_pct": 9.5},
    "buy": {"pct_min": -2.0, "pct_max": 6.0, "pullback_min": 2.0, "pullback_max": 8.0,
            "vol_shrink": 0.85, "newlow_minutes": 10, "support_vol_ratio": 1.1,
            "position_vs_prev": 1.5},
    "exit": {"force": -5.0, "warn": -3.0, "reduce": -2.0, "stop_factor": 0.95},
    "preopen": {"pct_min": 2.0, "pct_max": 9.5, "price_min": 3.0, "price_max": 50.0,
                "nmc_min_yi": 30.0, "nmc_max_yi": 400.0, "top": 15, "pool_size": 8},
    "sentiment": {"continuation": 2.0, "fade": -1.0},
}

# 运行时开关
_DATA_DIR = None
_OFFLINE = False

# 数据源降级记录：接口不可用时显式收集，供各阶段输出与 --json 透出（不静默、不缓存）。
_DEGRADED = []


def note_degraded(source, detail):
    _DEGRADED.append({"source": source, "detail": str(detail)})


def get_degraded():
    return list(_DEGRADED)


def clear_degraded():
    _DEGRADED.clear()


def set_data_dir(path):
    global _DATA_DIR
    _DATA_DIR = os.path.abspath(path) if path else None


def get_data_dir():
    if _DATA_DIR:
        d = _DATA_DIR
    else:
        d = (os.environ.get("DINGPAN_DATA_DIR")
             or os.path.join(SKILL_ROOT, "output"))
    os.makedirs(d, exist_ok=True)
    return d


def set_offline(flag):
    global _OFFLINE
    _OFFLINE = bool(flag)


def is_offline():
    return _OFFLINE


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def _deep_update(base, patch):
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v
    return base


def default_config():
    return {
        "account": {"total_capital": 0},
        "datasource": {"mode": "live"},
        "calendar": copy.deepcopy(CALENDAR_BASELINE),
        "thresholds": copy.deepcopy(DEFAULT_THRESHOLDS),
    }


def config_path():
    return os.path.join(get_data_dir(), "config.json")


def load_config():
    cfg = default_config()
    p = config_path()
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                _deep_update(cfg, json.load(f))
        except Exception as e:
            raise RuntimeError("配置文件解析失败 %s: %s" % (p, e))
    return cfg


def save_config(cfg):
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def set_config_value(dotted_key, value):
    cfg = load_config()
    keys = dotted_key.split(".")
    node = cfg
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = _coerce(value)
    save_config(cfg)
    return cfg


def _coerce(v):
    if isinstance(v, str):
        s = v.strip()
        if s[:1] in ("[", "{"):
            try:
                return json.loads(s)
            except ValueError:
                pass
        if s.lower() in ("true", "false"):
            return s.lower() == "true"
        try:
            if "." in s:
                return float(s)
            return int(s)
        except ValueError:
            return s
    return v


def thresholds(cfg=None):
    return (cfg or load_config())["thresholds"]


# ---------------------------------------------------------------------------
# 状态文件：持仓 / 候选池 / 主线存档 / 观察记录
# ---------------------------------------------------------------------------
def positions_path():
    return os.path.join(get_data_dir(), "positions.json")


def load_positions():
    p = positions_path()
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return json.load(f).get("positions", [])


def save_positions(items):
    with open(positions_path(), "w", encoding="utf-8") as f:
        json.dump({"positions": items}, f, ensure_ascii=False, indent=2)


def add_position(code, cost, stop=None, target=None, shares=None, name=None):
    items = load_positions()
    items = [x for x in items if x.get("code") != normalize_code(code)]
    items.append({"code": normalize_code(code), "name": name or "",
                  "cost": float(cost),
                  "stop": float(stop) if stop not in (None, "") else None,
                  "target": float(target) if target not in (None, "") else None,
                  "shares": int(shares) if shares not in (None, "") else None})
    save_positions(items)
    return items


def remove_position(code):
    items = [x for x in load_positions() if x.get("code") != normalize_code(code)]
    save_positions(items)
    return items


def pool_path():
    return os.path.join(get_data_dir(), "候选池.txt")


def save_pool(rows):
    """候选池.txt 每行：code|名称|涨幅|价格|流通市值(亿)|板块（板块列留空待Agent回填）。"""
    with open(pool_path(), "w", encoding="utf-8") as f:
        for r in rows:
            f.write("%s|%s|%.2f|%.2f|%.1f|%s\n" % (
                r.get("code", ""), r.get("name", ""), r.get("pct", 0.0),
                r.get("price", 0.0), r.get("nmc_yi", 0.0), r.get("sector", "") or ""))


def load_pool():
    p = pool_path()
    if not os.path.exists(p):
        return []
    rows = []
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue
        rows.append({"code": normalize_code(parts[0]), "name": parts[1],
                     "pct": float(parts[2] or 0), "price": float(parts[3] or 0),
                     "nmc_yi": float(parts[4] or 0),
                     "sector": parts[5] if len(parts) > 5 else ""})
    return rows


def archive_path():
    return os.path.join(get_data_dir(), "主线存档.json")


def load_archive():
    p = archive_path()
    if not os.path.exists(p):
        return {"days": []}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_archive(archive):
    with open(archive_path(), "w", encoding="utf-8") as f:
        json.dump(archive, f, ensure_ascii=False, indent=2)


def upsert_archive_day(date_str, sectors):
    arch = load_archive()
    arch["days"] = [d for d in arch.get("days", []) if d.get("date") != date_str]
    arch["days"].append({"date": date_str, "sectors": sectors})
    arch["days"] = sorted(arch["days"], key=lambda d: d["date"])[-10:]
    save_archive(arch)
    return arch


def watch_record_path():
    return os.path.join(get_data_dir(), "观察记录.json")


def append_watch_record(rec):
    p = watch_record_path()
    data = {"records": []}
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    data.setdefault("records", []).append(rec)
    data["records"] = data["records"][-200:]
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def http_get(url, encoding="utf-8", headers=None, timeout=8, retries=2):
    h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
         "Referer": "https://finance.sina.com.cn"}
    if headers:
        h.update(headers)
    last = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode(encoding, "ignore")
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError("请求失败 %s -> %s" % (url, last))


# ---------------------------------------------------------------------------
# 代码归一化
# ---------------------------------------------------------------------------
def normalize_code(code):
    code = str(code).strip().lower()
    if code[:2] in ("sh", "sz", "bj"):
        return code
    digits = re.sub(r"\D", "", code)
    if not digits:
        return code
    if digits.startswith(("60", "68", "9", "5", "11", "13")):
        return "sh" + digits
    if digits.startswith(("00", "30", "12", "15", "16", "18", "20")):
        return "sz" + digits
    if digits.startswith(("4", "8", "92")):
        return "bj" + digits
    return "sh" + digits


def em_secid(code):
    c = normalize_code(code)
    market = {"sh": "1", "sz": "0", "bj": "0"}.get(c[:2], "1")
    return "%s.%s" % (market, c[2:])


def is_bj(code):
    return normalize_code(code).startswith("bj")


# ---------------------------------------------------------------------------
# 离线样例读取
# ---------------------------------------------------------------------------
def _sample(name):
    p = os.path.join(SAMPLE_DIR, name)
    if not os.path.exists(p):
        raise RuntimeError("离线样例缺失: %s" % p)
    with open(p, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 行情取数
# ---------------------------------------------------------------------------
def fetch_realtime(code):
    c = normalize_code(code)
    if _OFFLINE:
        data = _sample("realtime.json")
        if c not in data:
            raise RuntimeError("离线样例无 %s 实时行情" % c)
        d = dict(data[c])
        d["code"] = c
        return d
    txt = http_get("http://qt.gtimg.cn/q=" + c, encoding="gbk")
    m = re.search(r'="(.*)"', txt)
    if not m:
        raise RuntimeError("行情解析失败: %s" % txt[:80])
    f = m.group(1).split("~")

    def num(i):
        try:
            return float(f[i])
        except (IndexError, ValueError):
            return 0.0
    return {"code": c, "name": f[1] if len(f) > 1 else c,
            "price": num(3), "prev_close": num(4), "open": num(5),
            "high": num(33), "low": num(34), "pct": num(32),
            "amount_wan": num(37), "volume_shou": num(36), "nmc_yi": num(44),
            "time": f[30] if len(f) > 30 else ""}


def fetch_minute(code):
    """腾讯分时。返回 [{t, minutes, price, volume, avg}]。"""
    c = normalize_code(code)
    if _OFFLINE:
        data = _sample("minute.json")
        if c not in data:
            raise RuntimeError("离线样例无 %s 分时" % c)
        rows = data[c]
    else:
        txt = http_get("http://web.ifzq.gtimg.cn/appstock/app/minute/query?code=" + c)
        js = json.loads(txt)
        node = js.get("data", {}).get(c, {})
        rows = node.get("data", {}).get("data", [])
    out = []
    for r in rows:
        if isinstance(r, str):
            p = r.split()
            if len(p) < 3:
                continue
            t = p[0]
            out.append({"t": t, "minutes": int(t[:2]) * 60 + int(t[2:4]),
                        "price": float(p[1]), "volume": float(p[2]),
                        "avg": float(p[3]) if len(p) > 3 else None})
        else:
            out.append({"t": r.get("t", ""), "minutes": int(r.get("minutes", 0)),
                        "price": float(r.get("price", 0)), "volume": float(r.get("volume", 0)),
                        "avg": r.get("avg")})
    return out


def fetch_daily(code, count=120):
    """腾讯前复权日K。返回 [{date, open, close, high, low, volume, pct}]。"""
    c = normalize_code(code)
    if _OFFLINE:
        data = _sample("daily.json")
        rows = data.get(c, [])
    else:
        url = ("http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq"
               % (c, count))
        js = json.loads(http_get(url))
        node = js.get("data", {}).get(c, {})
        rows = node.get("qfqday") or node.get("day") or []
    out = []
    for r in rows:
        if isinstance(r, str):
            continue
        if len(r) < 6:
            continue
        out.append({"date": r[0], "open": float(r[1]), "close": float(r[2]),
                    "high": float(r[3]), "low": float(r[4]), "volume": float(r[5])})
    for i, d in enumerate(out):
        prev = out[i - 1]["close"] if i > 0 else d["open"]
        d["pct"] = (d["close"] / prev - 1) * 100 if prev else 0.0
    return out


def _rt_date(rt):
    """从实时行情 time 字段取当日日期（YYYY-MM-DD）；缺失时回退系统日期。"""
    m = re.match(r"(\d{4})(\d{2})(\d{2})", str((rt or {}).get("time") or ""))
    return "%s-%s-%s" % m.groups() if m else datetime.now().strftime("%Y-%m-%d")


def splice_today(daily, rt):
    """把当日实时拼接到日K，使末根代表"今日"（盘中当日 bar 可能缺失/不完整）。

    末根若已是今日则用实时覆盖 close/high/low/pct，否则追加一根今日 bar。
    返回新列表，不修改入参。供 trend_ok（MA5今/昨、zt5）与 blocked（首阴"昨日涨幅"）统一取数。
    """
    if not rt:
        return list(daily)
    today = _rt_date(rt)
    close = rt.get("price") or rt.get("prev_close") or 0
    if not close:
        return list(daily)
    pct = rt.get("pct")
    if pct is None:
        prev_close = rt.get("prev_close") or 0
        pct = (close / prev_close - 1) * 100 if prev_close else 0.0
    bar = {"date": today, "open": rt.get("open") or close, "close": close,
           "high": rt.get("high") or close, "low": rt.get("low") or close,
           "volume": rt.get("volume_shou") or 0, "pct": float(pct)}
    out = list(daily)
    if out and out[-1].get("date") == today:
        out[-1] = bar
    else:
        out.append(bar)
    return out


def fetch_sina_spot_all(pages=6, num=100, sort="changepercent"):
    """新浪全市场快照，返回原始 dict 列表（含 nmc 万元、changepercent 等）。"""
    if _OFFLINE:
        return _sample("sina_spot.json")
    rows = []
    for pg in range(1, pages + 1):
        url = ("http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               "Market_Center.getHQNodeData?page=%d&num=%d&sort=%s&asc=0&node=hs_a"
               "&symbol=&_s_r_a=page" % (pg, num, sort))
        txt = http_get(url, encoding="gbk").strip()
        if not txt or txt == "null":
            break
        try:
            arr = json.loads(txt)
        except ValueError:
            break
        if not arr:
            break
        rows.extend(arr)
    return rows


def fetch_board_rank(limit=80):
    """东方财富行业板块排行。返回 [{code,name,pct,amount_yi,rank}]。"""
    if _OFFLINE:
        return _sample("board_rank.json")
    url = ("http://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=%d&po=1&np=1"
           "&fltt=2&invt=2&fid=f3&fs=m:90+t:2&fields=f12,f14,f3,f6" % limit)
    js = json.loads(http_get(url))
    diffs = (js.get("data") or {}).get("diff") or []
    out = []
    for i, d in enumerate(diffs):
        try:
            pct = float(d.get("f3") if d.get("f3") is not None else 0)
            amt = float(d.get("f6") if d.get("f6") is not None else 0) / 1e8
        except (TypeError, ValueError):
            pct, amt = 0.0, 0.0
        out.append({"code": d.get("f12"), "name": d.get("f14"),
                    "pct": pct, "amount_yi": amt, "rank": i + 1})
    return out


def fetch_limit_up_pool(date_str=None):
    """东方财富涨停板股池。返回 [{code,name}]；接口不可用时显式降级并返回 []。"""
    if _OFFLINE:
        return _sample("limit_up.json").get(date_str or "today", [])
    d = date_str or datetime.now().strftime("%Y%m%d")
    try:
        url = ("http://push2ex.eastmoney.com/getTopicZTPool"
               "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
               "&Pageindex=0&pagesize=180&sort=fbt:asc&date=%s" % d)
        js = json.loads(http_get(url))
        pool = ((js.get("data") or {}).get("pool")) or []
        return [{"code": normalize_code(str(x.get("c", ""))), "name": x.get("n", "")}
                for x in pool]
    except Exception as e:  # noqa: BLE001
        note_degraded("东财涨停池", e)
        return []


def fetch_stock_sector(code):
    """东方财富个股所属行业板块名；接口不可用时显式降级并返回 None（由 Agent 用业务知识回填）。"""
    if _OFFLINE:
        return _sample("sector_map.json").get(normalize_code(code))
    try:
        url = ("http://push2.eastmoney.com/api/qt/stock/get?secid=%s&fields=f127"
               % em_secid(code))
        js = json.loads(http_get(url))
        return (js.get("data") or {}).get("f127")
    except Exception as e:  # noqa: BLE001
        note_degraded("东财个股板块", e)
        return None


def fetch_board_members(board_code, limit=400):
    """东方财富板块成分股。返回 [{code,name,price,pct}]；接口不可用时显式降级并返回 []。"""
    if _OFFLINE:
        return _sample("board_members.json").get(board_code, [])
    try:
        url = ("http://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=%d&po=1&np=1"
               "&fltt=2&invt=2&fid=f3&fs=b:%s&fields=f2,f3,f12,f14" % (limit, board_code))
        js = json.loads(http_get(url))
    except Exception as e:  # noqa: BLE001
        note_degraded("东财板块成分股", e)
        return []
    diffs = (js.get("data") or {}).get("diff") or []
    out = []
    for d in diffs:
        try:
            price = float(d.get("f2") or 0)
            pct = float(d.get("f3") or 0)
        except (TypeError, ValueError):
            continue
        out.append({"code": normalize_code(str(d.get("f12") or "")),
                    "name": d.get("f14"), "price": price, "pct": pct})
    return out


def resolve_board_code(sector, boards=None):
    """把板块名解析为东财板块代码：先精确匹配，再包含匹配；无则返回 None。"""
    if not sector:
        return None
    boards = boards if boards is not None else fetch_board_rank(400)
    for b in boards:
        if b.get("name") == sector:
            return b.get("code")
    for b in boards:
        if sector in (b.get("name") or ""):
            return b.get("code")
    return None


def sector_strongest(sector, code, pool=None):
    """检索同板块最强票（按涨幅最大者的现价）——【后排不买】判据。

    优先检索东财板块成分股；板块无对应或接口不可用时回退候选池同板块。
    返回最强票的现价(float)；无同板块数据返回 None。
    """
    self_code = normalize_code(code)
    if not sector:
        return None
    try:
        board_code = resolve_board_code(sector)
        if board_code:
            members = fetch_board_members(board_code)
            peers = [m for m in members
                     if m["code"] != self_code and m.get("price")]
            if peers:
                return max(peers, key=lambda m: m.get("pct", 0))["price"]
    except Exception as e:  # noqa: BLE001
        note_degraded("东财板块成分股", e)
    if pool:
        peers = [p for p in pool
                 if p.get("sector") == sector and p["code"] != self_code]
        if peers:
            return max(peers, key=lambda p: p.get("pct", 0)).get("price")
    return None


# ---------------------------------------------------------------------------
# 公式与指标
# ---------------------------------------------------------------------------
def ma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def count_zt(daily, n, pct_threshold):
    return sum(1 for d in daily[-n:] if d.get("pct", 0) >= pct_threshold)


def consecutive_zt(daily, pct_threshold):
    """连板数 = 自末尾向前连续满足涨幅≥阈值的天数。"""
    cnt = 0
    for d in reversed(daily):
        if d.get("pct", 0) >= pct_threshold:
            cnt += 1
        else:
            break
    return cnt


def vwap(minute):
    """VWAP = 累计成交额 / (累计成交量 × 100)。此处用每分钟均价(元/股)加权，等价。"""
    vol = sum(m["volume"] for m in minute)
    if vol <= 0:
        return None
    amount = sum((m.get("avg") or m["price"]) * m["volume"] for m in minute)
    return amount / vol


# ---- 一、大盘红绿灯 ----
def market_light(index_daily=None, cfg=None):
    th = thresholds(cfg)["light"]
    if index_daily is None:
        index_daily = fetch_daily("sh000001", 60)
    closes = [d["close"] for d in index_daily]
    if len(closes) < 20:
        raise RuntimeError("指数日K不足 20 日，无法判定红绿灯（数据源不可用）")
    last = closes[-1]
    p5, p10, p20 = th["ma_periods"]
    m5, m10, m20 = ma(closes, 5), ma(closes, 10), ma(closes, 20)
    red_line = m20 * th["red_line_factor"]
    if last >= m20:
        light = "green"
    elif last >= m10:
        light = "yellow"
    else:
        light = "red"
    if last < red_line:
        light = "red"
        severe = True
    else:
        severe = False
    return {"light": light, "index": last, "ma5": m5, "ma10": m10, "ma20": m20,
            "red_line": red_line, "severe": severe,
            "cap": th["cap"][light], "single": th["single"][light]}


LIGHT_CN = {"green": "🟢 绿灯", "yellow": "🟡 黄灯", "red": "🔴 红灯"}


# ---- 二、主线判定 ----
def score_days(archive, cfg=None):
    """近 N 个交易日各板块累计得分。返回 [{sector, score, days:[{date,pct,amount_yi,rank}]}]。"""
    th = thresholds(cfg)["mainline"]
    days = archive.get("days", [])[-th["days"]:]
    agg = {}
    for day in days:
        for s in day.get("sectors", []):
            name = s.get("name")
            if not name:
                continue
            item = agg.setdefault(name, {"sector": name, "score": 0, "days": []})
            pts = 0
            if s.get("rank") and s["rank"] <= th["topn"]:
                pts += 2
            if s.get("pct", 0) >= th["pct_min"] and s.get("amount_yi", 0) >= th["amount_yi"]:
                pts += 1
            item["score"] += pts
            item["days"].append({"date": day["date"], "pct": s.get("pct"),
                                 "amount_yi": s.get("amount_yi"), "rank": s.get("rank"),
                                 "pts": pts})
    result = sorted(agg.values(), key=lambda x: x["score"], reverse=True)
    for it in result:
        if it["score"] >= th["score_star"]:
            it["level"] = "主线"
        elif it["score"] >= th["score_watch"]:
            it["level"] = "观察"
        else:
            it["level"] = "轮动"
    return result, len(days)


# ---- 四、趋势确认 ----
def trend_ok(daily, price, cfg=None):
    th = thresholds(cfg)["trend"]
    zt_pct = th["zt_pct"]
    closes = [d["close"] for d in daily]
    if len(closes) < 11:
        return {"ok": False, "reason": "日K不足", "up": False, "zt5": False}
    ma5_today = ma(closes, 5)
    ma5_yesterday = sum(closes[-6:-1]) / 5
    ma10 = ma(closes, 10)
    up = ma5_today > ma5_yesterday and price > ma5_today
    zt5 = any(d["pct"] >= zt_pct for d in daily[-5:])
    ok = (up or zt5) and price > ma10
    return {"ok": ok, "up": up, "zt5": zt5, "ma5": ma5_today, "ma10": ma10}


# ---- 五、买点 ----
def buy_signal(minute, rt, daily, sector_rebound=False, cfg=None):
    th = thresholds(cfg)["buy"]
    result = {"form_ok": False, "position_ok": False, "level": None, "items": {}}
    if not minute:
        result["reason"] = "无分时数据"
        return result
    prices = [m["price"] for m in minute]
    vols = [m["volume"] for m in minute]
    cur = rt.get("price") or prices[-1]
    high_idx = max(range(len(prices)), key=lambda i: prices[i])
    high = prices[high_idx]
    pullback = (high - cur) / high * 100 if high else 0.0
    pct = rt.get("pct", 0.0)
    result["pullback"] = pullback
    result["pct"] = pct
    result["form_ok"] = (th["pct_min"] <= pct <= th["pct_max"]
                         and th["pullback_min"] <= pullback <= th["pullback_max"])

    # ① 缩量
    rise = vols[:high_idx + 1]
    fall = vols[high_idx:]
    rise_avg = sum(rise) / len(rise) if rise else 0
    fall_avg = sum(fall) / len(fall) if fall else 0
    shrink = fall_avg <= rise_avg * th["vol_shrink"]

    # ② 不再创新低（以当日最低点为基准，出现后 >=10 分钟未再破）
    low_idx = min(range(len(prices)), key=lambda i: prices[i])
    low = prices[low_idx]
    last_min = minute[-1]["minutes"]
    gap = last_min - minute[low_idx]["minutes"]
    no_new_low = (cur > low) and gap >= th["newlow_minutes"]

    # ③ 分时承接
    p3ago = prices[-4] if len(prices) >= 4 else prices[0]
    v_last3 = sum(vols[-3:]) if len(vols) >= 3 else sum(vols)
    v_prev3 = sum(vols[-6:-3]) if len(vols) >= 6 else 0
    support = cur > p3ago and v_last3 > v_prev3 * th["support_vol_ratio"]

    # 位置
    prev_close = rt.get("prev_close") or 0
    vw = vwap(minute)
    near_prev = prev_close > 0 and abs(cur / prev_close - 1) * 100 <= th["position_vs_prev"]
    above_vwap = vw is not None and cur >= vw
    result["position_ok"] = bool(near_prev or above_vwap)

    items = {"缩量": shrink, "不再创新低": no_new_low, "分时承接": support, "板块回流": bool(sector_rebound)}
    result["items"] = items
    result["vwap"] = vw
    all_four = all(items.values())
    if result["form_ok"] and all_four and result["position_ok"]:
        result["level"] = "A"
    elif result["form_ok"] and result["position_ok"] and not all_four:
        result["level"] = "B"
    return result


# ---- 六、三不买 ----
def blocked(rt, daily, sector_strongest_price=None, cfg=None):
    hits = []
    price = rt.get("price", 0)
    prev_close = rt.get("prev_close", 0)
    closes = [d["close"] for d in daily]
    ma10 = ma(closes, 10)
    # 首阴用"昨日涨幅"：daily 末根可能是拼接的今日 bar，故剔除今日取最后一根完整日K。
    today = _rt_date(rt)
    past = [d for d in daily if d.get("date") != today]
    if past:
        y_pct = past[-1].get("pct", 0)
        if y_pct >= thresholds(cfg)["trend"]["zt_pct"] and prev_close and price < prev_close * 0.99:
            hits.append("首阴不买")
    if ma10 and price < ma10:
        hits.append("破位不买")
    if sector_strongest_price and price < sector_strongest_price * 0.99:
        hits.append("后排不买")
    return hits


# ---- 七、卖点 / 风控 ----
def exit_signal(pos, rt, light, cfg=None):
    th = thresholds(cfg)["exit"]
    price = rt.get("price", 0)
    cost = pos.get("cost") or 0
    if not cost:
        return None
    pnl = (price / cost - 1) * 100
    stop = pos.get("stop")
    target = pos.get("target")
    sig = None
    if stop and price <= stop:
        sig = ("🔴 卖出", "破自设止损", pnl)
    elif pnl <= th["force"]:
        sig = ("⛔ 强制止损", "无条件离场", pnl)
    elif pnl <= th["warn"]:
        sig = ("⚠️ 止损预警", "看卖压，破位即走", pnl)
    elif pnl <= th["reduce"] and light != "green":
        sig = ("⚠️ 减仓提示", "先减半仓", pnl)
    elif target and price >= target:
        sig = ("🟢 止盈", "达目标", pnl)
    return sig


# ---- 八、仓位 ----
def stop_price(rt):
    low = rt.get("low") or 0
    price = rt.get("price") or 0
    cands = [x for x in (low, price * thresholds()["exit"]["stop_factor"]) if x]
    return min(cands) if cands else 0.0


def position_plan(light_info, total_capital, code=None):
    cap = light_info["cap"]
    single = light_info["single"]
    amount = total_capital * single
    return {"light": light_info["light"], "cap_ratio": cap, "single_ratio": single,
            "max_amount": amount, "code": code}


# ---- 九、竞价选股 ----
def preopen_filter(rows, cfg=None, exclude_bj=True):
    th = thresholds(cfg)["preopen"]
    picked = []
    for r in rows:
        try:
            code = normalize_code(r.get("symbol") or r.get("code") or "")
            name = (r.get("name") or "").strip()
            price = float(r.get("trade") or 0)
            pct = float(r.get("changepercent") or 0)
            nmc_yi = float(r.get("nmc") or 0) / 1e4
        except (TypeError, ValueError):
            continue
        if not code or not name:
            continue
        if "ST" in name.upper() or "退" in name:
            continue
        if exclude_bj and (is_bj(code) or code[2:].startswith(("4", "8", "92"))):
            continue
        if not (th["pct_min"] <= pct <= th["pct_max"]):
            continue
        if not (th["price_min"] <= price <= th["price_max"]):
            continue
        if not (th["nmc_min_yi"] <= nmc_yi <= th["nmc_max_yi"]):
            continue
        picked.append({"code": code, "name": name, "pct": pct, "price": price,
                       "nmc_yi": nmc_yi, "sector": ""})
    picked.sort(key=lambda x: x["pct"], reverse=True)
    return picked[:th["top"]]


def continuation_state(pct, cfg=None):
    th = thresholds(cfg)["sentiment"]
    if pct >= th["continuation"]:
        return "✓ 主线延续"
    if pct >= th["fade"]:
        return "△ 分歧"
    return "✗ 退潮预警"


# ---------------------------------------------------------------------------
# 时段分流
# ---------------------------------------------------------------------------
def current_stage(now=None, cfg=None):
    now = now or datetime.now()
    try:
        cfg = cfg or load_config()
        holidays = set(cfg.get("calendar", {}).get("holidays", []) or [])
    except Exception:  # noqa: BLE001
        holidays = set()
    if now.weekday() >= 5 or now.strftime("%Y-%m-%d") in holidays:
        return "closed"
    hm = now.hour * 100 + now.minute
    if 925 <= hm < 935:
        return "preopen"
    if (935 <= hm <= 1130) or (1300 <= hm <= 1500):
        return "watch"
    if 1500 < hm < 1535:
        return "freeze"
    if 1535 <= hm <= 2359:
        return "daily"
    return "closed"


# ---------------------------------------------------------------------------
# 交易日历：临期检测与刷新指引（数据由 Agent 联网搜索后经 calendar set 落库）
# ---------------------------------------------------------------------------
def calendar_status(cfg=None):
    """交易日历覆盖状态：coverage_to / days_left / refresh_needed。"""
    cfg = cfg or load_config()
    cal = cfg.get("calendar", {}) or {}
    cov = cal.get("coverage_to") or ""
    try:
        lead = int(cal.get("lead_days", CALENDAR_BASELINE["lead_days"]))
    except (TypeError, ValueError):
        lead = CALENDAR_BASELINE["lead_days"]
    days_left, refresh = None, False
    if cov:
        try:
            days_left = (datetime.strptime(cov, "%Y-%m-%d").date() - datetime.now().date()).days
            refresh = days_left <= lead
        except ValueError:
            refresh = True
    else:
        refresh = True
    return {"holidays": len(cal.get("holidays") or []), "coverage_to": cov,
            "generated_at": cal.get("generated_at"), "source": cal.get("source"),
            "lead_days": lead, "days_left": days_left, "refresh_needed": refresh}


def calendar_refresh_hint(cfg=None):
    """临期/到期时的刷新指引；无需刷新返回 None。"""
    st = calendar_status(cfg)
    if not st["refresh_needed"]:
        return None
    try:
        year = datetime.strptime(st["coverage_to"], "%Y-%m-%d").year + 1
    except (ValueError, TypeError):
        year = datetime.now().year + 1
    left = st["days_left"]
    when = "已到期" if (left is not None and left < 0) else (
        "剩 %d 天" % left if left is not None else "未设置覆盖期")
    return ("交易日历%s（覆盖至 %s）：请联网搜索 %d 年 A股休市安排（沪深北交易所公告），"
            "再执行 python scripts/run.py calendar set holidays='[...]' "
            "coverage_to='%d-12-31' source='<来源>' 落库。"
            % (when, st["coverage_to"] or "无", year, year))


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def out(text, as_json=None):
    if as_json is not None:
        print(json.dumps(as_json, ensure_ascii=False, indent=2))
    else:
        print(text)


def hr(title=""):
    line = "─" * 40
    return ("\n" + title + "\n" + line) if title else line


def degraded_note():
    """把本次运行收集到的数据源降级汇总成一行；无降级返回 None。"""
    items = get_degraded()
    if not items:
        return None
    uniq, seen = [], set()
    for it in items:
        key = (it["source"], it["detail"])
        if key not in seen:
            seen.add(key)
            uniq.append(it)
    return "⚠️ 数据源降级：" + "；".join(
        "%s（%s）" % (u["source"], u["detail"]) for u in uniq)