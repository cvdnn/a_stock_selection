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


def _w(s, width):
    """按显示宽度右侧补空格（CJK 记 2 列），用于对齐表格列。"""
    text = str(s)
    w = sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)
    return text + " " * max(0, width - w)


def _fmt_fbt(v):
    """封板时间 HHMMSS(如 92500) → HH:MM；缺省返回 --。"""
    try:
        s = "%06d" % int(v)
        return "%s:%s" % (s[0:2], s[2:4])
    except (TypeError, ValueError):
        return "--"


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
    mth = C.thresholds(cfg)["mainline"]
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    C.clear_degraded()
    C.clear_cache_hits()

    light = C.market_light(cfg=cfg)

    boards = C.fetch_board_rank(80)
    msrc = C.rule_choice(cfg, "mainline_source")
    pause_mainline = (msrc == "pause")
    usable = bool(boards)
    if usable and msrc == "em" and C.board_source() != "em":
        usable = False  # 用户选择「仅东财」，但本次为新浪回退 → 视为不可用
    if usable and not pause_mainline:
        C.upsert_archive_day(today, boards[:20])
    archive = {"days": []} if pause_mainline else C.load_archive()
    mainlines, used_days = C.score_days(archive, cfg)
    # 待确认规则：主线数据源发生回退（口径与东财不同）→ mainline_source 待确认
    active = ["mainline_source"] if C.board_source() == "sina" else []
    pnote = C.pending_note(cfg, active)

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
        top = sorted(zt, key=lambda x: (x.get("lbc") or 0, x.get("fund") or 0),
                     reverse=True)[:15]
        gradient = {}
        for x in zt:
            g = x.get("lbc") or 1
            gradient[g] = gradient.get(g, 0) + 1
        cont = {"available": True, "count": len(zt), "top": top,
                "gradient": gradient}

    result = {"time": now.strftime("%Y-%m-%d %H:%M"), "stage": "daily",
              "light": light, "archive_days": used_days, "mainlines": mainlines,
              "mainline_source": C.board_source(), "positions": pos_recs,
              "limit_up": cont, "pending_rules": active,
              "degraded": C.get_degraded(), "cache": C.get_cache_hits()}

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
    if pause_mainline:
        lines.append("  · 已按待确认规则「主线数据源=pause」暂停：本轮不计分、不写入存档")
    elif msrc == "em" and C.board_source() != "em":
        lines.append("  · 已按待确认规则「主线数据源=em」留空：东财板块排行不可用")
    elif mainlines:
        lines.append("    " + _w("板块", 16) + _w("级别", 8) + "得分")
        for m in mainlines[:8]:
            lines.append("    " + _w(m["sector"], 16) + _w(m["level"], 8) + str(m["score"]))
        lines.append("  ── 得分说明：每日 板块涨幅进 TOP%d → +2；涨幅≥%.1f%% 且 成交额≥%.0f亿 → +1；"
                     "近 %d 日累计求和；≥%d ⭐主线 / %d-%d 👁观察 / ≤1 ↻轮动"
                     % (mth["topn"], mth["pct_min"], mth["amount_yi"], used_days,
                        mth["score_star"], mth["score_watch"], mth["score_star"] - 1))
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
        lines.append("  · 今日涨停 %d 只，按连板梯队（梯度）降序排列 · 前列：" % cont["count"])
        lines.append("    " + _w("梯度", 6) + _w("代码", 10) + _w("名称", 12)
                     + _w("行业", 14) + _w("封板", 7) + _w("封单(亿)", 10)
                     + _w("换手%", 8) + "炸板")
        for x in cont["top"]:
            lbc = x.get("lbc")
            fund = x.get("fund")
            hs = x.get("hs")
            zbc = x.get("zbc")
            lines.append("    " + _w(("%d板" % lbc) if lbc else "--", 6)
                         + _w(x.get("code", ""), 10)
                         + _w(x.get("name", ""), 12)
                         + _w(x.get("hybk") or "--", 14)
                         + _w(_fmt_fbt(x.get("fbt")), 7)
                         + _w("%.2f" % (fund / 1e8) if isinstance(fund, (int, float)) else "--", 10)
                         + _w("%.2f" % hs if isinstance(hs, (int, float)) else "--", 8)
                         + str(zbc if zbc is not None else "--"))
        grad = cont.get("gradient") or {}
        if grad:
            lines.append("  · 梯队分布：%s"
                         % "｜".join("%d板×%d" % (g, grad[g]) for g in sorted(grad, reverse=True)))
            lines.append("  · 说明：梯度＝连板数（末尾连续涨停天数），越高越靠前＝连板龙头候选；"
                         "封单＝收盘涨停封单额，换手＝当日换手率，炸板＝盘中开板次数；"
                         "能否参与仍须过 红绿灯/主线 过滤。")
    else:
        lines.append("  · 涨停股池数据源不可用，次日方向需人工判断")
    if mainlines:
        star = [m["sector"] for m in mainlines if m["level"] == "主线"]
        if star:
            lines.append("  · 主线候选：%s（明日按 龙头三条件 跟踪）" % "、".join(star))
    if pnote:
        lines.append("\n" + pnote)
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