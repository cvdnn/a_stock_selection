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
import hashlib
import urllib.request
import urllib.parse
import concurrent.futures
from datetime import datetime, timedelta

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = os.path.join(SKILL_ROOT, "assets")
SAMPLE_DIR = os.path.join(ASSETS_DIR, "sample")
# 唯一公式依据（只读）；selfcheck 用它做安装后校验与基线比对。
FORMULA_DOC = os.path.join(SKILL_ROOT, "docs", "定盘实时任务_公式清单.md")

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
                "nmc_min_yi": 30.0, "nmc_max_yi": 400.0, "top": 15, "pool_size": 8,
                "board_main_only": False},  # 仅主板过滤：默认关闭，与公式清单默认行为一致
    "sentiment": {"continuation": 2.0, "fade": -1.0},
}

# ---------------------------------------------------------------------------
# 数据缓存默认值 —— 可选（默认关闭）。用于同一时段内的二次快速响应：
# live 取数成功后落盘，TTL 内再次取用直接复用；命中会显式标注，绝不冒充实时。
# 按数据源差异化 TTL：变化慢的日K/板块类给更长 TTL，行情类保持短 TTL。
# 优先级：config.ttl[kind] > DEFAULT_CACHE_TTL[kind] > config.ttl_seconds > 60
# ---------------------------------------------------------------------------
DEFAULT_CACHE_TTL = {
    "realtime": 60,        # 实时行情：变化最快
    "minute": 60,          # 分时
    "sina_spot": 60,       # 全市场快照
    "board_rank": 120,     # 行业板块排行
    "board_members": 120,  # 板块成分股
    "limit_up": 300,       # 涨停池（日内基本稳定）
    "daily": 300,          # 日K（场内基本不变）
    "sector": 600,         # 个股所属板块（极少变动）
}
DEFAULT_CACHE = {"enabled": False, "ttl_seconds": 60, "ttl": {}}

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


# ---------------------------------------------------------------------------
# 数据缓存（可选，默认关闭）
# 设计要点（数据同步）：
#   1) 仅在 live 取数成功时写入；空结果/降级结果默认不缓存（cache_empty=False）。
#   2) TTL 到期即失效；实时行情额外按数据自身日期判活（跨日缓存不冒充今日）。
#   3) 命中记入 _CACHE_HITS 并在输出中显式标注，绝不把缓存冒充实时。
# ---------------------------------------------------------------------------
_CACHE_ENABLED = None   # None=未初始化（首次取数时读配置/环境）
_CACHE_TTL = 60         # 全局兜底 TTL
_CACHE_TTL_MAP = {}     # 按数据源覆盖（kind -> 秒）
_CACHE_HITS = []


def _cache_settings():
    global _CACHE_ENABLED, _CACHE_TTL, _CACHE_TTL_MAP
    if _CACHE_ENABLED is None:
        if os.environ.get("DINGPAN_NO_CACHE"):
            _CACHE_ENABLED = False
        else:
            try:
                cache = (load_config().get("datasource") or {}).get("cache") or {}
            except Exception:  # noqa: BLE001
                cache = {}
            _CACHE_ENABLED = bool(cache.get("enabled", False))
            try:
                _CACHE_TTL = int(cache.get("ttl_seconds", 60))
            except (TypeError, ValueError):
                _CACHE_TTL = 60
            m = cache.get("ttl")
            _CACHE_TTL_MAP = dict(m) if isinstance(m, dict) else {}
    return _CACHE_ENABLED, _CACHE_TTL, _CACHE_TTL_MAP


def set_cache(enabled, ttl=None):
    global _CACHE_ENABLED, _CACHE_TTL
    _CACHE_ENABLED = bool(enabled)
    if ttl is not None:
        _CACHE_TTL = int(ttl)


def _cache_ttl_for(kind):
    """解析某数据源的生效 TTL：config.ttl[kind] > DEFAULT_CACHE_TTL[kind] > 全局。"""
    _, glob, m = _cache_settings()
    if kind and kind in m:
        try:
            return int(m[kind])
        except (TypeError, ValueError):
            pass
    if kind and kind in DEFAULT_CACHE_TTL:
        return DEFAULT_CACHE_TTL[kind]
    return glob


def is_cache_enabled():
    return _cache_settings()[0]


def get_cache_hits():
    return list(_CACHE_HITS)


def clear_cache_hits():
    _CACHE_HITS.clear()


def _cache_dir():
    d = os.path.join(get_data_dir(), "cache")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_file(key):
    safe = re.sub(r"[^0-9A-Za-z_.-]", "_", key)
    return os.path.join(_cache_dir(), safe + ".json")


def cached(source, key, producer, ttl=None, kind=None, freshness=None, cache_empty=True):
    """带缓存的取数包装：命中 TTL 内且仍“新鲜”的缓存则直接复用，否则回源并落盘。

    - 缓存未启用时等价于直接 producer()。
    - kind 决定该数据源的默认 TTL（见 DEFAULT_CACHE_TTL / config.ttl）；ttl 显式覆盖。
    - freshness(payload)->bool 按数据自身时间判活（如实时行情的日期须为当日）。
    - cache_empty=False 时不缓存空结果，避免把降级/失败写进缓存。
    """
    enabled, _, _ = _cache_settings()
    if not enabled:
        return producer()
    ttl = ttl if ttl is not None else _cache_ttl_for(kind)
    p = _cache_file(key)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                node = json.load(f)
            age = datetime.now().timestamp() - float(node.get("ts", 0))
            if 0 <= age <= ttl and (freshness is None or freshness(node.get("payload"))):
                _CACHE_HITS.append({"source": source, "kind": kind, "key": key,
                                    "age": round(age, 1), "ttl": ttl})
                return node.get("payload")
        except Exception:  # noqa: BLE001
            pass
    payload = producer()
    if cache_empty or payload:
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"source": source, "kind": kind, "key": key, "ttl": ttl,
                           "ts": datetime.now().timestamp(),
                           "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                           "payload": payload}, f, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass
    return payload


def cache_clear():
    d = os.path.join(get_data_dir(), "cache")
    n = 0
    if os.path.isdir(d):
        for name in os.listdir(d):
            if name.endswith(".json"):
                try:
                    os.remove(os.path.join(d, name))
                    n += 1
                except OSError:
                    pass
    return n


def cache_status():
    d = os.path.join(get_data_dir(), "cache")
    enabled, glob_ttl, _ = _cache_settings()
    now = datetime.now().timestamp()
    items = []
    if os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, name), encoding="utf-8") as f:
                    node = json.load(f)
            except Exception:  # noqa: BLE001
                continue
            age = now - float(node.get("ts", 0))
            try:
                ttl = int(node.get("ttl", glob_ttl))
            except (TypeError, ValueError):
                ttl = glob_ttl
            items.append({"source": node.get("source"), "kind": node.get("kind"),
                          "key": node.get("key"), "time": node.get("time"),
                          "ttl": ttl, "age": round(age, 1), "expired": age > ttl})
    return {"enabled": enabled, "ttl_seconds": glob_ttl, "dir": d,
            "count": len(items), "items": items}


def cache_note():
    """把本次运行命中缓存的情况汇总成一行；无命中返回 None。"""
    if not _CACHE_HITS:
        return None
    uniq, seen = [], set()
    for h in _CACHE_HITS:
        if h["key"] not in seen:
            seen.add(h["key"])
            uniq.append(h)
    return ("⚡ 命中数据缓存 %d 项（TTL 内复用，非强制刷新；如需最新请 "
            "run.py cache clear 后重跑）" % len(uniq))


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
        "datasource": {"mode": "live", "cache": copy.deepcopy(DEFAULT_CACHE)},
        "calendar": copy.deepcopy(CALENDAR_BASELINE),
        "thresholds": copy.deepcopy(DEFAULT_THRESHOLDS),
        "confirmations": {},
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


# ---------------------------------------------------------------------------
# 待确认规则（冲突/歧义时不擅自决定：提示用户选择；未选前按默认继续评估）
# ---------------------------------------------------------------------------
CONFIRM_RULES = [
    {
        "id": "backrow_basis",
        "title": "「后排不买」判据口径",
        "question": "「后排不买」用哪种口径判定同板块强弱？（清单原文按绝对股价，存在语义歧义）",
        "options": [
            {"key": "price", "label": "按绝对股价：现价 < 同板块最强票价 × 0.99（清单原文）"},
            {"key": "pct", "label": "按涨幅：现价涨幅 < 同板块最强涨幅"},
        ],
        "default": "price",
    },
    {
        "id": "mainline_source",
        "title": "主线板块数据源",
        "question": "东财板块排行不可用、已回退新浪行业（分类/命名与东财不同）时，主线以哪个口径为准？",
        "options": [
            {"key": "sina", "label": "接受回退新浪行业（当前行为）"},
            {"key": "pause", "label": "暂停主线判定：不计分、不写入存档（待东财恢复）"},
            {"key": "em", "label": "仅用东财：不可用则主线留空"},
        ],
        "default": "sina",
    },
]


def rule_choice(cfg, rule_id):
    """返回该规则的已选值；未选返回默认值。"""
    for r in CONFIRM_RULES:
        if r["id"] == rule_id:
            val = (cfg.get("confirmations") or {}).get(rule_id)
            if val in [o["key"] for o in r["options"]]:
                return val
            return r["default"]
    return None


def is_rule_confirmed(cfg, rule_id):
    """该规则是否已被用户显式选择。"""
    for r in CONFIRM_RULES:
        if r["id"] == rule_id:
            val = (cfg.get("confirmations") or {}).get(rule_id)
            return val in [o["key"] for o in r["options"]]
    return True


def pending_note(cfg, active_ids):
    """为"活跃且尚未确认"的规则生成待确认提示；无则返回 ''。

    active_ids：本次运行实际触发了这些规则（由各阶段按运行态传入）。
    """
    pend = [r for r in CONFIRM_RULES
            if r["id"] in active_ids and not is_rule_confirmed(cfg, r["id"])]
    if not pend:
        return ""
    lines = ["⚠️ 待确认规则（冲突/歧义，请选择后继续评估；未选前按【默认】继续）："]
    for i, r in enumerate(pend, 1):
        lines.append("  %d. [%s] %s" % (i, r["id"], r["title"]))
        lines.append("     问：%s" % r["question"])
        for o in r["options"]:
            star = "（默认）" if o["key"] == r["default"] else ""
            lines.append("       %s) %s %s" % (o["key"], o["label"], star))
        lines.append("     选择：run.py config set confirmations.%s=<%s>"
                     % (r["id"], "/".join(o["key"] for o in r["options"])))
    return "\n".join(lines)


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
# 离线样例 schema / 校验 / 生成
# 说明：assets/sample/ 是“开发者夹具”，仅供 --offline 演示与 CI 验证；
#       普通用户不应手改 JSON —— 统一用 `run.py sample verify|gen` 生成与校验。
# ---------------------------------------------------------------------------
SAMPLE_FILES = ["realtime.json", "minute.json", "daily.json", "sina_spot.json",
                "board_rank.json", "board_members.json", "sector_map.json",
                "limit_up.json"]

# 各文件字段/单位契约（供 Agent 生成与校验的共同依据）。
SAMPLE_SCHEMA = {
    "realtime": {"file": "realtime.json", "shape": "map",
                 "keys": ["name", "price", "prev_close", "open", "high", "low",
                          "pct", "amount_wan", "volume_shou", "nmc_yi", "time"],
                 "units": {"nmc_yi": "亿", "amount_wan": "万元",
                           "volume_shou": "手", "time": "YYYYMMDDHHMMSS"}},
    "minute": {"file": "minute.json", "shape": "map",
               "row": "HHMM price volume avg"},
    "daily": {"file": "daily.json", "shape": "map",
              "row": ["date", "open", "close", "high", "low", "volume"]},
    "sina_spot": {"file": "sina_spot.json", "shape": "list",
                  "keys": ["symbol", "code", "name", "trade", "changepercent",
                           "nmc", "open", "high", "low", "settlement", "volume",
                           "amount"],
                  "units": {"nmc": "万元", "amount": "元"}},
    "board_rank": {"file": "board_rank.json", "shape": "list",
                   "keys": ["code", "name", "pct", "amount_yi", "rank"],
                   "units": {"amount_yi": "亿", "code": "BKxxxx"}},
    "board_members": {"file": "board_members.json", "shape": "map",
                      "item_keys": ["code", "name", "price", "pct"]},
    "sector_map": {"file": "sector_map.json", "shape": "map", "value": "板块名"},
    "limit_up": {"file": "limit_up.json", "shape": "map",
                 "item_keys": ["code", "name"]},
}

# 生成用内置股票池（name/sector/board/base 现价基准）。
_SAMPLE_UNIVERSE = {
    "sh600000": {"name": "浦发银行", "sector": "银行", "board": "BK0475", "base": 10.0},
    "sz000001": {"name": "平安银行", "sector": "银行", "board": "BK0475", "base": 8.0},
    "sh600111": {"name": "北方稀土", "sector": "稀土永磁", "board": "BK1027", "base": 25.0},
    "sz000002": {"name": "万科A", "sector": "房地产开发", "board": "BK0421", "base": 9.0},
    "sh600519": {"name": "贵州茅台", "sector": "白酒", "board": "BK0477", "base": 1400.0},
    "sh600036": {"name": "招商银行", "sector": "银行", "board": "BK0475", "base": 55.0},
}
_SAMPLE_DEFAULT_CODES = ["sh600000", "sz000001", "sh600111", "sz000002"]
# 当日涨幅（用于让行情自洽：部分进候选池、部分进涨停池）。
_SAMPLE_TODAY_PCT = {"sh600000": 4.8, "sz000001": 3.2, "sh600111": 9.9,
                     "sz000002": -1.5, "sh600519": 3.0, "sh600036": 9.8}
_SAMPLE_INDEX = "sh000001"


def _rng(seed_str):
    import random
    seed = int(hashlib.sha256(seed_str.encode("utf-8")).hexdigest()[:16], 16)
    return random.Random(seed)


def _load_sample_file(name):
    p = os.path.join(SAMPLE_DIR, name)
    if not os.path.exists(p):
        return None, "文件缺失"
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f), None
    except Exception as e:  # noqa: BLE001
        return None, "解析失败: %s" % e


def _is_code(s):
    return bool(re.match(r"^(sh|sz|bj)\d{6}$", str(s or "")))


def sample_verify():
    """校验离线样例：字段/单位/行序契约 + 跨文件一致性。返回结构化报告。"""
    rep = {"ok": True, "dir": SAMPLE_DIR, "files": {}, "consistency_issues": []}

    def rec(name, count, issues):
        rep["files"][name] = {"count": count, "issues": issues}
        if issues:
            rep["ok"] = False

    # realtime.json
    data, err = _load_sample_file("realtime.json")
    issues, cnt = [], 0
    if err:
        issues.append(err)
    else:
        cnt = len(data)
        for code, d in data.items():
            if not _is_code(code):
                issues.append("%s 代码格式异常" % code)
            if not isinstance(d, dict):
                issues.append("%s 结构应为对象" % code)
                continue
            miss = [k for k in SAMPLE_SCHEMA["realtime"]["keys"] if k not in d]
            if miss:
                issues.append("%s 缺字段 %s" % (code, ",".join(miss)))
            if not re.match(r"^\d{14}$", str(d.get("time") or "")):
                issues.append("%s time 非 YYYYMMDDHHMMSS" % code)
    rec("realtime.json", cnt, issues)

    # minute.json
    data, err = _load_sample_file("minute.json")
    issues, cnt = [], 0
    if err:
        issues.append(err)
    else:
        cnt = len(data)
        for code, rows in data.items():
            if not _is_code(code):
                issues.append("%s 代码格式异常" % code)
            if not isinstance(rows, list) or not rows:
                issues.append("%s 分时为空" % code)
                continue
            for r in rows[:3]:
                parts = str(r).split()
                if len(parts) < 4 or not re.match(r"^\d{4}$", parts[0]):
                    issues.append("%s 行格式应为 'HHMM price volume avg'" % code)
                    break
    rec("minute.json", cnt, issues)

    # daily.json
    data, err = _load_sample_file("daily.json")
    issues, cnt = [], 0
    if err:
        issues.append(err)
    else:
        cnt = len(data)
        for code, rows in data.items():
            if not _is_code(code):
                issues.append("%s 代码格式异常" % code)
            if not isinstance(rows, list) or len(rows) < 20:
                issues.append("%s 日K不足 20 根" % code)
                continue
            for r in rows:
                if not isinstance(r, list) or len(r) < 6:
                    issues.append("%s 行应为 [date,open,close,high,low,volume]" % code)
                    break
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(r[0])):
                    issues.append("%s 日期格式异常: %s" % (code, r[0]))
                    break
                try:
                    float(r[1]), float(r[2]), float(r[3]), float(r[4])
                except (TypeError, ValueError):
                    issues.append("%s 存在非数值行情" % code)
                    break
    rec("daily.json", cnt, issues)

    # sina_spot.json
    data, err = _load_sample_file("sina_spot.json")
    issues, cnt = [], 0
    if err:
        issues.append(err)
    else:
        if not isinstance(data, list):
            issues.append("结构应为数组")
        else:
            cnt = len(data)
            for d in data:
                miss = [k for k in SAMPLE_SCHEMA["sina_spot"]["keys"] if k not in d]
                if miss:
                    issues.append("%s 缺字段 %s" % (d.get("symbol"), ",".join(miss)))
                try:
                    float(d.get("trade")), float(d.get("changepercent")), float(d.get("nmc"))
                except (TypeError, ValueError):
                    issues.append("%s 行情字段非数值" % d.get("symbol"))
    rec("sina_spot.json", cnt, issues)

    # board_rank.json
    data, err = _load_sample_file("board_rank.json")
    issues, cnt = [], 0
    if err:
        issues.append(err)
    else:
        cnt = len(data) if isinstance(data, list) else 0
        if not isinstance(data, list):
            issues.append("结构应为数组")
        else:
            for d in data:
                miss = [k for k in SAMPLE_SCHEMA["board_rank"]["keys"] if k not in d]
                if miss:
                    issues.append("%s 缺字段 %s" % (d.get("code"), ",".join(miss)))
    rec("board_rank.json", cnt, issues)

    # board_members.json
    data, err = _load_sample_file("board_members.json")
    issues, cnt = [], 0
    if err:
        issues.append(err)
    else:
        cnt = len(data)
        for bcode, members in data.items():
            if not isinstance(members, list):
                issues.append("%s 结构应为数组" % bcode)
                continue
            for m in members:
                miss = [k for k in SAMPLE_SCHEMA["board_members"]["item_keys"] if k not in m]
                if miss:
                    issues.append("%s/%s 缺字段 %s"
                                  % (bcode, m.get("code"), ",".join(miss)))
    rec("board_members.json", cnt, issues)

    # sector_map.json
    data, err = _load_sample_file("sector_map.json")
    issues, cnt = [], 0
    if err:
        issues.append(err)
    else:
        cnt = len(data)
        for code, sec in data.items():
            if not _is_code(code):
                issues.append("%s 代码格式异常" % code)
            if not isinstance(sec, str) or not sec:
                issues.append("%s 板块名缺失" % code)
    rec("sector_map.json", cnt, issues)

    # limit_up.json
    data, err = _load_sample_file("limit_up.json")
    issues, cnt = [], 0
    if err:
        issues.append(err)
    else:
        cnt = len(data)
        for key, rows in data.items():
            if not isinstance(rows, list):
                issues.append("%s 结构应为数组" % key)
                continue
            for x in rows:
                miss = [k for k in SAMPLE_SCHEMA["limit_up"]["item_keys"] if k not in x]
                if miss:
                    issues.append("%s/%s 缺字段 %s" % (key, x.get("code"), ",".join(miss)))
    rec("limit_up.json", cnt, issues)

    # ---- 跨文件一致性 ----
    ci = rep["consistency_issues"]
    rt = {}
    data, _ = _load_sample_file("realtime.json")
    if isinstance(data, dict):
        for code, d in data.items():
            if isinstance(d, dict):
                rt[normalize_code(code)] = (float(d.get("price") or 0),
                                            float(d.get("pct") or 0),
                                            float(d.get("nmc_yi") or 0))
    spot = {}
    data, _ = _load_sample_file("sina_spot.json")
    if isinstance(data, list):
        for d in data:
            try:
                spot[normalize_code(d.get("symbol") or d.get("code"))] = (
                    float(d.get("trade") or 0), float(d.get("changepercent") or 0),
                    float(d.get("nmc") or 0) / 1e4)
            except (TypeError, ValueError):
                continue
    members = {}
    data, _ = _load_sample_file("board_members.json")
    if isinstance(data, dict):
        for members_list in data.values():
            for m in members_list or []:
                try:
                    members[normalize_code(m.get("code"))] = (
                        float(m.get("price") or 0), float(m.get("pct") or 0))
                except (TypeError, ValueError):
                    continue
    for code in sorted(set(rt) & set(spot)):
        (p1, c1, n1), (p2, c2, n2) = rt[code], spot[code]
        if abs(p1 - p2) > 0.02:
            ci.append("%s 价格不一致：realtime %.2f vs sina_spot %.2f" % (code, p1, p2))
        if abs(c1 - c2) > 0.2:
            ci.append("%s 涨幅不一致：realtime %.2f%% vs sina_spot %.2f%%" % (code, c1, c2))
        if abs(n1 - n2) > max(1.0, abs(n1) * 0.05):
            ci.append("%s 流通市值不一致：realtime %.1f亿 vs sina_spot %.1f亿"
                      % (code, n1, n2))
    for code in sorted(set(rt) & set(members)):
        (p1, c1, _), (p2, c2) = rt[code], members[code]
        if abs(p1 - p2) > 0.02:
            ci.append("%s 价格不一致：realtime %.2f vs board_members %.2f" % (code, p1, p2))
        if abs(c1 - c2) > 0.2:
            ci.append("%s 涨幅不一致：realtime %.2f%% vs board_members %.2f%%" % (code, c1, c2))
    data, _ = _load_sample_file("limit_up.json")
    if isinstance(data, dict):
        for rows in data.values():
            for x in rows or []:
                code = normalize_code(x.get("code"))
                if code in rt and rt[code][1] < 9.5:
                    ci.append("%s 在涨停池但涨幅仅 %.2f%%，与涨停语义不符" % (code, rt[code][1]))
    if ci:
        rep["ok"] = False
    return rep


def _weekdays_before(date_str, n):
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    out = []
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
    return list(reversed(out))


def _gen_daily_rows(code, base, end_date, days=120):
    rng = _rng(code + "-daily")
    prev_close = base / (1 + _SAMPLE_TODAY_PCT.get(code, 3.0) / 100.0)
    closes, level = [], prev_close * 0.85
    for _ in range(days):
        level *= (1 + 0.0015) * (1 + rng.uniform(-0.008, 0.010))
        closes.append(level)
    if code.endswith("600111"):          # 高标：近端连板，供涨停记忆/情绪演示
        c = closes[-4]
        for k in (3, 2, 1):
            c = c * 1.099
            closes[-k] = c
    scale = prev_close / closes[-1]
    closes = [round(c * scale, 2) for c in closes]
    rows, prev = [], closes[0]
    for dt, c in zip(_weekdays_before(end_date, days), closes):
        o = round(prev * (1 + rng.uniform(-0.005, 0.005)), 2)
        h = round(max(o, c) * (1 + rng.uniform(0, 0.010)), 2)
        l = round(min(o, c) * (1 - rng.uniform(0, 0.010)), 2)
        rows.append([dt, "%.2f" % o, "%.2f" % c, "%.2f" % h,
                     "%.2f" % l, str(int(rng.uniform(80000, 160000)))])
        prev = c
    return rows


def _gen_minute(code, open_, high, price):
    rng = _rng(code + "-minute")
    rows, mid, mins = [], 40, 61
    for i in range(mins):
        t = 9 * 60 + 30 + i
        if i <= mid:
            p = open_ + (high - open_) * ((i / mid) ** 0.8)
        else:
            p = high - (high - price) * ((i - mid) / (mins - 1 - mid))
        p = round(p, 2)
        rows.append("%02d%02d %.2f %d %.3f"
                    % (t // 60, t % 60, p, int(rng.uniform(2000, 5000)), p))
    return rows


def sample_gen(codes=None, date_str=None):
    """按 schema 生成一套自洽的离线样例（确定性、无需联网），覆盖跨文件一致性。"""
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    datetime.strptime(date_str, "%Y-%m-%d")  # 校验格式
    codes = [normalize_code(c) for c in (codes or _SAMPLE_DEFAULT_CODES)]

    realtime, minute, daily = {}, {}, {}
    spot, sector_map, members = [], {}, {}
    board_pct = {}

    # 指数日K（红绿灯需要 >=20 根）
    daily[_SAMPLE_INDEX] = _gen_daily_rows(
        _SAMPLE_INDEX, 3100.0, date_str, days=120)

    for code in codes:
        uni = _SAMPLE_UNIVERSE.get(code, {})
        name = uni.get("name") or ("样例%s" % code[2:])
        sector = uni.get("sector") or "综合"
        board = uni.get("board") or "BK0000"
        base = uni.get("base") or 10.0
        rows = _gen_daily_rows(code, base, date_str)
        daily[code] = rows
        prev_close = float(rows[-1][2])
        pct = round(_SAMPLE_TODAY_PCT.get(code, 3.0), 2)
        rng = _rng(code + "-rt")
        price = round(prev_close * (1 + pct / 100.0), 2)
        open_ = round(prev_close * (1 + rng.uniform(-0.010, 0.010)), 2)
        high = round(max(open_, price) * (1 + rng.uniform(0, 0.008)), 2)
        low = round(min(open_, price) * (1 - rng.uniform(0, 0.008)), 2)
        nmc_yi = round(rng.uniform(60, 360), 1)
        amount_wan = round(rng.uniform(20000, 60000), 1)
        volume_shou = int(rng.uniform(100000, 600000))
        realtime[code] = {
            "name": name, "price": price, "prev_close": prev_close, "open": open_,
            "high": high, "low": low, "pct": pct, "amount_wan": amount_wan,
            "volume_shou": volume_shou, "nmc_yi": nmc_yi,
            "time": date_str.replace("-", "") + "103000"}
        minute[code] = _gen_minute(code, open_, high, price)
        spot.append({"symbol": code, "code": code[2:], "name": name,
                     "trade": "%.2f" % price, "changepercent": "%.2f" % pct,
                     "nmc": "%.0f" % (nmc_yi * 1e4), "open": "%.2f" % open_,
                     "high": "%.2f" % high, "low": "%.2f" % low,
                     "settlement": "%.2f" % prev_close,
                     "volume": str(volume_shou), "amount": "%.0f" % (amount_wan * 1e4)})
        sector_map[code] = sector
        members.setdefault(board, []).append(
            {"code": code, "name": name, "price": price, "pct": pct})
        board_pct.setdefault(board, []).append(pct)

    board_names = {v.get("board"): v.get("sector")
                   for v in _SAMPLE_UNIVERSE.values()}
    board_rank = []
    for bcode, pcts in board_pct.items():
        board_rank.append({"code": bcode, "name": board_names.get(bcode) or "综合",
                           "pct": round(sum(pcts) / len(pcts), 2),
                           "amount_yi": round(60 + 20 * len(pcts), 1), "rank": 0})
    board_rank.sort(key=lambda b: b["pct"], reverse=True)
    for i, b in enumerate(board_rank, 1):
        b["rank"] = i

    limit_up = {"today": [{"code": c, "name": realtime[c]["name"]}
                          for c in codes if realtime[c]["pct"] >= 9.5]}

    payloads = {
        "realtime.json": realtime, "minute.json": minute, "daily.json": daily,
        "sina_spot.json": spot, "board_rank.json": board_rank,
        "board_members.json": members, "sector_map.json": sector_map,
        "limit_up.json": limit_up,
    }
    for name, payload in payloads.items():
        with open(os.path.join(SAMPLE_DIR, name), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)

    return {"date": date_str, "codes": codes, "files": list(payloads),
            "limit_up_count": len(limit_up["today"])}


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

    def _do():
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
    # 实时行情按数据自身日期判活：跨日缓存不得冒充今日。
    return cached("腾讯实时", "realtime_%s" % c, _do, kind="realtime",
                  freshness=lambda d: _rt_date(d) == datetime.now().strftime("%Y-%m-%d"))


def fetch_minute(code):
    """腾讯分时。返回 [{t, minutes, price, volume, avg}]。"""
    c = normalize_code(code)
    if _OFFLINE:
        data = _sample("minute.json")
        if c not in data:
            raise RuntimeError("离线样例无 %s 分时" % c)
        rows = data[c]
    else:
        def _do():
            txt = http_get("http://web.ifzq.gtimg.cn/appstock/app/minute/query?code=" + c)
            js = json.loads(txt)
            node = js.get("data", {}).get(c, {})
            return node.get("data", {}).get("data", [])
        rows = cached("腾讯分时", "minute_%s" % c, _do, kind="minute")
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


def _fetch_daily_sina(code, count):
    """新浪日K兜底（腾讯日K不可用时）。返回腾讯 fqkline 同构行 [date,open,close,high,low,volume]。"""
    c = normalize_code(code)
    url = ("http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=%d"
           % (c, max(count, 20)))
    arr = json.loads(http_get(url, encoding="utf-8").strip() or "[]")
    rows = []
    for it in arr or []:
        rows.append([it.get("day"), it.get("open"), it.get("close"),
                     it.get("high"), it.get("low"), it.get("volume")])
    return rows


def fetch_daily(code, count=120):
    """腾讯前复权日K。返回 [{date, open, close, high, low, volume, pct}]。

    腾讯日K不可用时显式降级回退新浪日K（仅替换数据源，不改任何公式/阈值）。
    """
    c = normalize_code(code)
    if _OFFLINE:
        data = _sample("daily.json")
        rows = data.get(c, [])
    else:
        def _do():
            url = ("http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq"
                   % (c, count))
            js = json.loads(http_get(url))
            node = js.get("data", {}).get(c, {})
            return node.get("qfqday") or node.get("day") or []
        try:
            rows = cached("腾讯日K", "daily_%s_%d" % (c, count), _do, kind="daily")
        except Exception as e:  # noqa: BLE001
            note_degraded("腾讯日K", "%s；已回退新浪日K" % e)
            rows = cached("新浪日K", "daily_sina_%s_%d" % (c, count),
                          lambda: _fetch_daily_sina(c, count), kind="daily")
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

    def _do():
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
    return cached("新浪快照", "sina_spot_%d_%d_%s" % (pages, num, sort), _do,
                  kind="sina_spot")


_SINA_HY_URL = "http://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"


def _parse_sina_board_list(txt):
    """解析新浪板块清单 JS（newSinaHy.php / newFLJK.php）。

    字段：code,name,家数,均价,涨跌额,涨跌幅%,成交量,成交额(元),领涨股...
    返回 [{code,name,pct,amount_yi,rank}]（按涨幅降序）。
    """
    m = re.search(r"=\s*(\{.*\})", txt, re.S)
    if not m:
        raise RuntimeError("新浪板块解析失败")
    obj = json.loads(m.group(1))
    out = []
    for val in obj.values():
        f = str(val).split(",")
        if len(f) < 8:
            continue
        try:
            pct = float(f[5])
            amt = float(f[7]) / 1e8  # 成交额单位为元 → 亿
        except ValueError:
            continue
        out.append({"code": f[0], "name": f[1], "pct": pct,
                    "amount_yi": amt, "rank": 0})
    out.sort(key=lambda x: x["pct"], reverse=True)
    for i, d in enumerate(out):
        d["rank"] = i + 1
    return out


def _fetch_board_rank_sina(limit=80):
    """新浪行业板块排行兜底（东财板块排行不可用时），口径与东财行业板块一致。"""
    return _parse_sina_board_list(http_get(_SINA_HY_URL, encoding="gbk"))[:limit]


def _fetch_board_members_sina(node, limit=400):
    """新浪板块成分股（node=新浪 node 代码，如 new_blhy）。返回 [{code,name,price,pct}]。"""
    url = ("http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "Market_Center.getHQNodeData?page=1&num=%d&sort=changepercent&asc=0"
           "&node=%s&symbol=&_s_r_a=page" % (limit, node))
    arr = json.loads(http_get(url, encoding="gbk").strip() or "[]")
    out = []
    for d in arr or []:
        try:
            price = float(d.get("trade") or 0)
            pct = float(d.get("changepercent") or 0)
        except (TypeError, ValueError):
            continue
        out.append({"code": normalize_code(str(d.get("symbol") or d.get("code") or "")),
                    "name": d.get("name"), "price": price, "pct": pct})
    return out


_SINA_HY_INDEX = None
_SINA_HY_INDEX_TTL = 7 * 86400


def _sina_hy_index():
    """新浪行业「个股↔行业」索引（只存成员关系，价格查询时实时另取）。

    仅在东财个股板块不可用时构建，为「后排不买」提供同源（新浪行业）的
    个股→行业（by_code）与 行业→node（by_name）映射，避免跨数据源分类不一致。
    首次构建并发拉取各行业成分股并落盘缓存（7 天），后续直接复用。
    """
    global _SINA_HY_INDEX
    if _SINA_HY_INDEX is not None:
        return _SINA_HY_INDEX
    p = os.path.join(_cache_dir(), "sina_hy_index.json")
    try:
        if os.path.exists(p):
            node = json.load(open(p, encoding="utf-8"))
            age = datetime.now().timestamp() - float(node.get("ts", 0))
            if 0 <= age <= _SINA_HY_INDEX_TTL and node.get("by_name"):
                _SINA_HY_INDEX = {"by_name": node["by_name"], "by_code": node["by_code"]}
                return _SINA_HY_INDEX
    except Exception:  # noqa: BLE001
        pass
    try:
        boards = _parse_sina_board_list(http_get(_SINA_HY_URL, encoding="gbk"))
    except Exception as e:  # noqa: BLE001
        note_degraded("新浪行业索引", e)
        boards = []

    def _one(b):
        try:
            members = _fetch_board_members_sina(b["code"], 1000)
        except Exception:  # noqa: BLE001
            return None
        return b["name"], b["code"], [m["code"] for m in members]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = [r for r in ex.map(_one, boards) if r]
    by_name, by_code = {}, {}
    for name, code, codes in results:
        by_name[name] = code
        for cc in codes:
            by_code[cc] = name
    _SINA_HY_INDEX = {"by_name": by_name, "by_code": by_code}
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"ts": datetime.now().timestamp(), "by_name": by_name,
                       "by_code": by_code}, f, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass
    return _SINA_HY_INDEX


_BOARD_SOURCE = None


def board_source():
    """本次运行「主线板块」实际取数来源：'em'（东财）/ 'sina'（新浪回退）/ None（未取）。"""
    return _BOARD_SOURCE


def fetch_board_rank(limit=80):
    """东方财富行业板块排行。返回 [{code,name,pct,amount_yi,rank}]。

    东财接口不可用/返回空时，显式降级回退新浪行业板块（仅替换数据源，
    不改任何公式/阈值；两源同为「行业板块 + 涨跌幅 + 成交额」口径）。
    """
    global _BOARD_SOURCE
    if _OFFLINE:
        return _sample("board_rank.json")

    def _do_em():
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

    em_err = None
    rows = []
    try:
        rows = cached("东财板块排行", "board_rank_%d" % limit, _do_em,
                      kind="board_rank", cache_empty=False)
    except Exception as e:  # noqa: BLE001
        em_err = e
    if rows:
        _BOARD_SOURCE = "em"
        return rows
    note_degraded("东财板块排行", "%s；已回退新浪行业板块" % (em_err or "返回空"))
    rows = cached("新浪板块排行", "board_rank_sina_%d" % limit,
                  lambda: _fetch_board_rank_sina(limit),
                  kind="board_rank", cache_empty=False)
    if rows:
        _BOARD_SOURCE = "sina"
    return rows


def fetch_limit_up_pool(date_str=None):
    """东方财富涨停板股池。返回 [{code,name,lbc,fbt,zbc,hs,fund,hybk,zt_days,zt_count}]；
    接口不可用时显式降级并返回 []。lbc=连板数，fbt=首次封板时间(HHMMSS)，zbc=炸板次数，
    hs=换手率(%)，fund=封单资金(元)，hybk=行业板块，zt_days/zt_count=涨停统计(如 3天2板)。"""
    if _OFFLINE:
        return _sample("limit_up.json").get(date_str or "today", [])
    d = date_str or datetime.now().strftime("%Y%m%d")

    def _do():
        try:
            url = ("http://push2ex.eastmoney.com/getTopicZTPool"
                   "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
                   "&Pageindex=0&pagesize=180&sort=fbt:asc&date=%s" % d)
            js = json.loads(http_get(url))
            pool = ((js.get("data") or {}).get("pool")) or []
            out = []
            for x in pool:
                zttj = x.get("zttj") if isinstance(x.get("zttj"), dict) else {}
                out.append({"code": normalize_code(str(x.get("c", ""))),
                            "name": x.get("n", ""),
                            "lbc": x.get("lbc"),
                            "fbt": x.get("fbt"),
                            "zbc": x.get("zbc"),
                            "hs": x.get("hs"),
                            "fund": x.get("fund"),
                            "hybk": x.get("hybk"),
                            "zt_days": zttj.get("days"),
                            "zt_count": zttj.get("ct")})
            return out
        except Exception as e:  # noqa: BLE001
            note_degraded("东财涨停池", e)
            return []
    # 空池多为降级结果，不写入缓存（cache_empty=False）。
    return cached("东财涨停池", "limit_up_%s" % d, _do, kind="limit_up", cache_empty=False)


def fetch_stock_sector(code):
    """个股所属行业板块名。

    东财不可用时回退新浪行业（同源，用于「后排不买」同板块判定）；仍无则返回
    None（由 Agent 用业务知识回填）。
    """
    c = normalize_code(code)
    if _OFFLINE:
        return _sample("sector_map.json").get(c)

    def _do():
        try:
            url = ("http://push2.eastmoney.com/api/qt/stock/get?secid=%s&fields=f127"
                   % em_secid(code))
            js = json.loads(http_get(url))
            return (js.get("data") or {}).get("f127")
        except Exception as e:  # noqa: BLE001
            note_degraded("东财个股板块", "%s；已回退新浪行业" % e)
            return None
    name = cached("东财个股板块", "sector_%s" % c, _do, kind="sector", cache_empty=False)
    if not name:
        name = _sina_hy_index()["by_code"].get(c)
    return name


def fetch_board_members(board_code, limit=400):
    """板块成分股。返回 [{code,name,price,pct}]。

    新浪 node 代码（new_*/hangye_*/gn_*）走新浪；其余（东财 BK*）走东财；
    不可用时显式降级并返回 []。
    """
    if _OFFLINE:
        return _sample("board_members.json").get(board_code, [])
    code = str(board_code or "")
    if code.startswith(("new_", "hangye_", "gn_")):
        return cached("新浪板块成分股", "members_sina_%s_%d" % (code, limit),
                      lambda: _fetch_board_members_sina(code, limit),
                      kind="board_members", cache_empty=False)

    def _do():
        try:
            url = ("http://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=%d&po=1&np=1"
                   "&fltt=2&invt=2&fid=f3&fs=b:%s&fields=f2,f3,f12,f14"
                   % (limit, board_code))
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
    return cached("东财板块成分股", "board_members_%s_%d" % (board_code, limit), _do,
                  kind="board_members", cache_empty=False)


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


def sector_strongest_pick(sector, code, pool=None):
    """检索同板块最强票（按涨幅最大者），返回 {"code","price","pct"}；无则 None。

    【后排不买】判据取数：优先板块成分股（东财；不可用时回退新浪行业同源链）；
    板块无对应或不可用时回退候选池同板块。
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
                return max(peers, key=lambda m: m.get("pct", 0))
    except Exception as e:  # noqa: BLE001
        note_degraded("东财板块成分股", e)
    if pool:
        peers = [p for p in pool
                 if p.get("sector") == sector and p["code"] != self_code]
        if peers:
            return max(peers, key=lambda p: p.get("pct", 0))
    return None


def sector_strongest(sector, code, pool=None):
    """同板块最强票现价(float)；无同板块数据返回 None。"""
    pick = sector_strongest_pick(sector, code, pool)
    return pick.get("price") if pick else None


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
def blocked(rt, daily, strongest=None, sector_strongest_price=None, cfg=None):
    """三不买自动拦截。strongest=同板块最强票 dict{price,pct} 或现价(float)。

    「后排」口径按待确认规则 backrow_basis（默认 price=绝对股价；可选 pct=涨幅）。
    """
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
    if strongest is None and sector_strongest_price is not None:
        strongest = sector_strongest_price
    if isinstance(strongest, (int, float)):
        strongest = {"price": strongest}
    if strongest and strongest.get("price"):
        basis = rule_choice(cfg, "backrow_basis") if cfg else "price"
        if basis == "pct":
            if (rt.get("pct") or 0) < (strongest.get("pct") or 0):
                hits.append("后排不买")
        elif price < strongest["price"] * 0.99:
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
        # 可选：仅主板（沪主板 sh60* / 深主板 sz00*），自动排除创业板 sz30* 与科创板 sh68*
        if th.get("board_main_only"):
            body = code[2:]
            is_main = (code[:2] == "sh" and body.startswith("60")) \
                      or (code[:2] == "sz" and body.startswith("00"))
            if not is_main:
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
# 公式清单校验（安装后自动校验 / 与安装环境基线比对）
# ---------------------------------------------------------------------------
def _read_text(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8")


def _parse_sections(text):
    """按 `## ` 二级标题切分章节，返回 {标题: 正文}；摘要（标题前内容）忽略。"""
    sections, cur, buf = {}, None, []
    for line in text.splitlines():
        if line.startswith("## "):
            if cur is not None:
                sections[cur] = "\n".join(buf).strip()
            cur = line[3:].strip()
            buf = []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        sections[cur] = "\n".join(buf).strip()
    return sections


def _extract_version(text):
    m = re.search(r"公式清单（(v[^）]+)）", text)
    return m.group(1) if m else None


def formula_list_check(path=None):
    """检查一份公式清单是否存在/可读，并给出指纹（版本/行数/章节/sha256）。"""
    path = path or FORMULA_DOC
    info = {"path": path, "exists": False, "readable": False,
            "version": None, "lines": 0, "sha256": None, "sections": []}
    if not os.path.exists(path):
        return info
    info["exists"] = True
    try:
        raw = open(path, "rb").read()
        text = raw.decode("utf-8")
    except Exception:
        return info
    info["readable"] = True
    info["version"] = _extract_version(text)
    info["lines"] = text.count("\n") + 1
    info["sha256"] = hashlib.sha256(raw).hexdigest()
    info["sections"] = list(_parse_sections(text).keys())
    return info


def formula_list_diff(baseline_path, project_path=None):
    """比对「安装环境基线清单」与「项目自带清单」，按章节给出差异。

    口径：环境基线 = 用户期望/基线依据；项目清单 = 当前实现依据。
    """
    project_path = project_path or FORMULA_DOC
    b, p = formula_list_check(baseline_path), formula_list_check(project_path)
    res = {"baseline": baseline_path, "project": project_path,
           "baseline_version": b["version"], "project_version": p["version"],
           "consistent": False, "added_sections": [], "removed_sections": [],
           "changed_sections": [], "unchanged_sections": []}
    if not (b["exists"] and b["readable"] and p["exists"] and p["readable"]):
        return res
    bs, ps = _parse_sections(_read_text(baseline_path)), _parse_sections(_read_text(project_path))
    norm = lambda s: re.sub(r"\s+", "", s)
    res["added_sections"] = [k for k in ps if k not in bs]       # 仅项目有
    res["removed_sections"] = [k for k in bs if k not in ps]     # 仅环境有（项目缺）
    res["changed_sections"] = [k for k in bs if k in ps and norm(bs[k]) != norm(ps[k])]
    res["unchanged_sections"] = [k for k in bs if k in ps and norm(bs[k]) == norm(ps[k])]
    res["consistent"] = not (res["added_sections"] or res["removed_sections"]
                             or res["changed_sections"])
    return res


# ---------------------------------------------------------------------------
# 版本与发布（VERSION 为唯一事实源；最新版本取自 Gitee/GitHub 的 release）
#   版本号形如 v1、v2、v3 … vN（整数递增），与 git tag 同名。
#   下载采用「tag 源码归档」，无需自建 release 资产。
# ---------------------------------------------------------------------------
VERSION_FILE = os.path.join(SKILL_ROOT, "VERSION")
REPO_OWNER = "cvdnn"
REPO_NAME = "a_stock_selection"
# 探测顺序：Gitee 为主、GitHub 为镜像（国内网络更稳）。
DEFAULT_RELEASE_ORDER = ("gitee", "github")
RELEASE_SOURCES = {
    "gitee": {
        "api": "https://gitee.com/api/v5/repos/{owner}/{repo}/releases/latest",
        "archive": "https://gitee.com/{owner}/{repo}/repository/archive/{tag}?format=zip",
        "home": "https://gitee.com/{owner}/{repo}/releases",
    },
    "github": {
        "api": "https://api.github.com/repos/{owner}/{repo}/releases/latest",
        "archive": "https://github.com/{owner}/{repo}/archive/refs/tags/{tag}.tar.gz",
        "home": "https://github.com/{owner}/{repo}/releases",
    },
}


def get_version():
    """读取 VERSION（唯一事实源），返回形如 'v3'；文件缺失或不可读返回 None。"""
    if not os.path.exists(VERSION_FILE):
        return None
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            return f.read().strip() or None
    except Exception:  # noqa: BLE001
        return None


def parse_version(v):
    """把 'v3'/'V12'/'3' 解析为整数 3/12；无法解析返回 None。"""
    m = re.match(r"^v?(\d+)", str(v or "").strip(), re.I)
    return int(m.group(1)) if m else None


def _probe_release(source):
    """查询单平台的最新 release，返回 {source, tag, name, url, published_at, archive}。"""
    if source not in RELEASE_SOURCES:
        raise ValueError("未知发布源: %s" % source)
    cfg = RELEASE_SOURCES[source]
    api = cfg["api"].format(owner=REPO_OWNER, repo=REPO_NAME)
    js = json.loads(http_get(api))
    tag = str(js.get("tag_name") or "").strip()
    if not tag:
        raise RuntimeError("最新 release 无 tag_name（可能尚未创建发行版）")
    return {
        "source": source,
        "tag": tag,
        "name": js.get("name") or "",
        "url": js.get("html_url") or cfg["home"].format(owner=REPO_OWNER, repo=REPO_NAME),
        "published_at": js.get("published_at") or js.get("created_at") or "",
        "archive": cfg["archive"].format(owner=REPO_OWNER, repo=REPO_NAME, tag=tag),
    }


def latest_release(order=DEFAULT_RELEASE_ORDER):
    """按 order 依次探测最新 release；返回 (release|None, errors[])。全部失败时 release=None。"""
    errors = []
    for source in order:
        try:
            return _probe_release(source), errors
        except Exception as e:  # noqa: BLE001
            errors.append({"source": source, "detail": str(e)})
    return None, errors


def check_update(local=None, order=DEFAULT_RELEASE_ORDER):
    """比对本地版本与最新 release。

    status: up_to_date / update_available / ahead / unavailable / unknown。
    网络不可达时 status=unavailable（显式降级，不报错中断）。
    """
    local = get_version() if local is None else local
    res = {"local": local, "latest": None, "source": None, "status": "unknown",
           "archive": None, "url": None, "published_at": None, "errors": []}
    rel, errors = latest_release(order)
    res["errors"] = errors
    if not rel:
        res["status"] = "unavailable"
        return res
    res["latest"] = rel["tag"]
    res["source"] = rel["source"]
    res["archive"] = rel["archive"]
    res["url"] = rel["url"]
    res["published_at"] = rel["published_at"]
    lv, rv = parse_version(local), parse_version(rel["tag"])
    if lv is None or rv is None:
        res["status"] = "unknown"
    elif rv > lv:
        res["status"] = "update_available"
    elif rv == lv:
        res["status"] = "up_to_date"
    else:
        res["status"] = "ahead"
    return res


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