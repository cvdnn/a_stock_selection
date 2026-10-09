# -*- coding: utf-8 -*-
"""watch.py — 盘中盯盘内核（9:35-11:30 / 13:00-15:00）。

覆盖公式清单：四（趋势）、五（买点四件套）、六（三不买）、七（卖点风控）。
"""
import os
import sys
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402


def sector_rebound(sector, pool, cfg):
    """买点④板块回流：同板块内 ≥1 只票“近3分钟回升”。"""
    if not sector:
        return False
    for p in pool:
        if p.get("sector") != sector:
            continue
        try:
            minute = C.fetch_minute(p["code"])
        except Exception:  # noqa: BLE001
            continue
        if len(minute) >= 4 and minute[-1]["price"] > minute[-4]["price"]:
            return True
    return False


def eval_candidate(pool_row, pool, light, cfg):
    code = pool_row["code"]
    rec = {"code": code, "name": pool_row["name"], "sector": pool_row.get("sector") or ""}
    try:
        rt = C.fetch_realtime(code)
        daily = C.fetch_daily(code, 80)
    except Exception as e:  # noqa: BLE001
        rec["error"] = str(e)
        return rec

    rec["price"] = rt.get("price")
    rec["pct"] = rt.get("pct")
    # 拼接当日实时，使 trend_ok 的 MA5今/昨、zt5 与 blocked 的首阴取数落在"今日"。
    daily = C.splice_today(daily, rt)
    trend = C.trend_ok(daily, rt.get("price") or 0, cfg)
    rec["trend"] = trend

    # 缺板块时自动识别（复用东财个股板块），保证「后排」「板块回流」可按同板块评估。
    if not rec["sector"]:
        try:
            rec["sector"] = C.fetch_stock_sector(code) or ""
        except Exception:  # noqa: BLE001
            rec["sector"] = ""

    rebound = sector_rebound(rec["sector"], pool, cfg) if rec["sector"] else False
    try:
        minute = C.fetch_minute(code)
    except Exception:  # noqa: BLE001
        minute = []
    buy = C.buy_signal(minute, rt, daily, sector_rebound=rebound, cfg=cfg)
    rec["buy"] = buy

    strong = C.sector_strongest(rec["sector"], code, pool) if rec["sector"] else None
    rec["sector_strongest_price"] = strong
    rec["blocked"] = C.blocked(rt, daily, sector_strongest_price=strong, cfg=cfg)
    return rec


def eval_position(pos, light, cfg):
    code = pos["code"]
    rec = {"code": code, "name": pos.get("name") or code}
    try:
        rt = C.fetch_realtime(code)
    except Exception as e:  # noqa: BLE001
        rec["error"] = str(e)
        return rec
    rec["price"] = rt.get("price")
    rec["name"] = pos.get("name") or rt.get("name") or code
    sig = C.exit_signal(pos, rt, light["light"], cfg)
    if sig:
        rec["signal"] = {"tag": sig[0], "reason": sig[1], "pnl": sig[2]}
    rec["stop_price"] = C.stop_price(rt)
    return rec


def main(argv=None):
    ap = argparse.ArgumentParser(description="盘中盯盘（watch）")
    ap.add_argument("--data-dir")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.data_dir:
        C.set_data_dir(args.data_dir)
    if args.offline:
        C.set_offline(True)

    cfg = C.load_config()
    now = datetime.now()
    C.clear_degraded()
    C.clear_cache_hits()
    light = C.market_light(cfg=cfg)
    positions = C.load_positions()
    pool = C.load_pool()

    candidates = [eval_candidate(p, pool, light, cfg) for p in pool]
    pos_recs = [eval_position(p, light, cfg) for p in positions]

    result = {"time": now.strftime("%Y-%m-%d %H:%M"), "stage": "watch",
              "light": light, "candidates": candidates, "positions": pos_recs,
              "degraded": C.get_degraded(), "cache": C.get_cache_hits()}

    if args.json:
        C.out("", as_json=result)
        return 0

    lines = [C.hr("【盯盘快照 · %s】" % now.strftime("%H:%M"))]
    lines.append("大盘：%s  上证 %.2f" % (C.LIGHT_CN[light["light"]], light["index"]))

    lines.append("\n候选买点：" if pool else "\n候选买点：候选池为空，请先运行 preopen")
    for r in candidates:
        if r.get("error"):
            lines.append("  · %s %s  数据缺失：%s" % (r["code"], r.get("name", ""), r["error"]))
            continue
        if not r["trend"]["ok"]:
            lines.append("  · %s %s  趋势不成立（硬门槛未过）" % (r["code"], r["name"]))
            continue
        b = r["buy"]
        items = "".join(("✅" if v else "⬜") + k for k, v in b["items"].items())
        level = b["level"] or "未达级"
        lines.append("  · %s %s  %s  形态%s 位置%s  %s"
                     % (r["code"], r["name"],
                        ("【买点%s级】" % level) if level != "未达级" else "【无买点】",
                        "达标" if b["form_ok"] else "不足",
                        "达标" if b["position_ok"] else "不足", items))
        if r["blocked"]:
            lines.append("      🚫 拦截：%s" % "、".join(r["blocked"]))

    lines.append("\n持仓风控：")
    if not positions:
        lines.append("  · 无持仓。如需卖点监控，请录入："
                     "run.py positions add <代码> cost=<成本> [stop=<自设止损>] [target=<目标价>]")
    for r in pos_recs:
        if r.get("error"):
            lines.append("  · %s 数据缺失：%s" % (r["code"], r["error"]))
            continue
        if r.get("signal"):
            s = r["signal"]
            lines.append("  · %s %s  现价 %.2f  浮盈亏 %.2f%%  %s（%s）"
                         % (r["code"], r["name"], r["price"], s["pnl"], s["tag"], s["reason"]))
        else:
            lines.append("  · %s %s  现价 %.2f  持有  止损参考 %.2f"
                         % (r["code"], r["name"], r["price"], r["stop_price"]))
    note = C.degraded_note()
    if note:
        lines.append("\n" + note)
    cnote = C.cache_note()
    if cnote:
        lines.append("\n" + cnote)
    C.out("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())