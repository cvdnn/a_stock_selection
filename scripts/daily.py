# -*- coding: utf-8 -*-
"""daily.py — 收盘复盘内核（15:35-23:59）。

覆盖公式清单：一（红绿灯）、二（主线计分存档）、七（持仓盈亏）、九（次日方向/情绪）。
"""
import os
import sys
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="收盘复盘（daily）")
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
    today = now.strftime("%Y-%m-%d")
    C.clear_degraded()
    C.clear_cache_hits()

    light = C.market_light(cfg=cfg)

    boards = C.fetch_board_rank(80)
    if boards:
        C.upsert_archive_day(today, boards[:20])
    archive = C.load_archive()
    mainlines, used_days = C.score_days(archive, cfg)

    positions = C.load_positions()
    pos_recs = []
    for p in positions:
        try:
            rt = C.fetch_realtime(p["code"])
            cost = p.get("cost") or 0
            pnl = (rt["price"] / cost - 1) * 100 if cost else None
            pos_recs.append({"code": p["code"], "name": p.get("name") or rt.get("name"),
                             "price": rt["price"], "pnl": pnl,
                             "signal": C.exit_signal(p, rt, light["light"], cfg)})
        except Exception as e:  # noqa: BLE001
            pos_recs.append({"code": p["code"], "error": str(e)})

    cont = {"available": False}
    zt = C.fetch_limit_up_pool()
    if zt:
        cont = {"available": True, "count": len(zt),
                "top": zt[:10]}

    result = {"time": now.strftime("%Y-%m-%d %H:%M"), "stage": "daily",
              "light": light, "archive_days": used_days, "mainlines": mainlines,
              "positions": pos_recs, "limit_up": cont, "degraded": C.get_degraded(),
              "cache": C.get_cache_hits()}

    if args.json:
        C.out("", as_json=result)
        return 0

    lines = [C.hr("【收盘复盘 · %s】" % today)]
    lines.append("大盘：%s  上证 %.2f  MA5 %.2f / MA10 %.2f / MA20 %.2f"
                 % (C.LIGHT_CN[light["light"]], light["index"],
                    light["ma5"] or 0, light["ma10"] or 0, light["ma20"] or 0))
    if light["severe"]:
        lines.append("⚠️ 已跌破红线（MA20×0.98），严重弱市，仓位上限 %d%%"
                     % (light["cap"] * 100))
    lines.append("\n主线计分（近 %d 日存档，⭐≥4 / 👁2-3 / ↻≤1）：" % used_days)
    if mainlines:
        for m in mainlines[:8]:
            lines.append("  · %-8s %s 得分 %d" % (m["sector"], m["level"], m["score"]))
    else:
        lines.append("  · 暂无（首个交易日存档已写入，明日开始累积）")
    lines.append("\n持仓复盘：")
    if not positions:
        lines.append("  · 无持仓")
    for r in pos_recs:
        if r.get("error"):
            lines.append("  · %s 数据缺失：%s" % (r["code"], r["error"]))
            continue
        pnl = "--" if r["pnl"] is None else "%.2f%%" % r["pnl"]
        lines.append("  · %s %s  收盘 %.2f  浮盈亏 %s" % (r["code"], r["name"], r["price"], pnl))
    lines.append("\n次日方向：")
    if cont.get("available"):
        lines.append("  · 今日涨停约 %d 只，重点关注其中连板高度与板块聚集度" % cont["count"])
        for x in cont["top"]:
            lines.append("     - %s %s" % (x["code"], x.get("name", "")))
    else:
        lines.append("  · 涨停股池数据源不可用，次日方向需人工判断")
    if mainlines:
        star = [m["sector"] for m in mainlines if m["level"] == "主线"]
        if star:
            lines.append("  · 主线候选：%s（明日按 龙头三条件 跟踪）" % "、".join(star))
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