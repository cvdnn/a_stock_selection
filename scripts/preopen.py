# -*- coding: utf-8 -*-
"""preopen.py — 竞价选股内核（9:25-9:35）。

覆盖公式清单：一（红绿灯）、二（主线）、三（龙头）、九（竞价选股）、板块列待 Agent 回填。
"""
import os
import sys
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402


def build_leaders(pool, cfg):
    """龙头三条件：②有记忆(zt60>=3 ★) 与 ③强度(连板数排序) 由脚本算；①正宗交给 Agent。"""
    th = C.thresholds(cfg)
    zt_pct = th["trend"]["zt_pct"]
    leaders = []
    for r in pool:
        item = dict(r)
        try:
            daily = C.fetch_daily(r["code"], 80)
            item["zt60"] = C.count_zt(daily, 60, zt_pct)
            item["lbc"] = C.consecutive_zt(daily, zt_pct)
            item["mem"] = item["zt60"] >= th["leader"]["zt60_min"]
        except Exception as e:  # noqa: BLE001
            item["zt60"] = None
            item["lbc"] = 0
            item["mem"] = None
            item["err"] = str(e)
        leaders.append(item)
    leaders.sort(key=lambda x: (x.get("lbc") or 0, x.get("zt60") or 0), reverse=True)
    return leaders


def continuation(pool, cfg):
    """昨日涨停延续度（判情绪退潮）。"""
    zt = C.fetch_limit_up_pool()
    if not zt:
        return {"available": False, "detail": "涨停股池数据源不可用，需人工判断"}
    states = {}
    samples = []
    for x in zt[:30]:
        try:
            rt = C.fetch_realtime(x["code"])
            st = C.continuation_state(rt.get("pct", 0), cfg)
            states[st] = states.get(st, 0) + 1
            samples.append({"code": x["code"], "name": x.get("name") or rt.get("name"),
                            "pct": rt.get("pct"), "state": st})
        except Exception:  # noqa: BLE001
            continue
    return {"available": True, "summary": states, "samples": samples}


def main(argv=None):
    ap = argparse.ArgumentParser(description="竞价选股（preopen）")
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
    archive = C.load_archive()
    mainlines, used_days = C.score_days(archive, cfg)

    spot = C.fetch_sina_spot_all()
    picked15 = C.preopen_filter(spot, cfg)
    pool = picked15[:C.thresholds(cfg)["preopen"]["pool_size"]]
    for p in pool:
        try:
            p["sector"] = C.fetch_stock_sector(p["code"]) or ""
        except Exception:  # noqa: BLE001
            p["sector"] = ""
    C.save_pool(pool)

    leaders = build_leaders(pool, cfg)
    cont = continuation(pool, cfg)

    total_capital = cfg["account"]["total_capital"]
    plan = C.position_plan(light, total_capital)

    result = {"time": now.strftime("%Y-%m-%d %H:%M"), "stage": "preopen",
              "light": light, "mainlines": mainlines, "archive_days": used_days,
              "pool": pool, "leaders": leaders, "continuation": cont,
              "position_plan": plan, "degraded": C.get_degraded(),
              "cache": C.get_cache_hits()}

    if args.json:
        C.out("", as_json=result)
        return 0

    lines = [C.hr("【竞价选股 · %s】" % now.strftime("%H:%M"))]
    lines.append("大盘：%s  上证 %.2f  MA10 %.2f / MA20 %.2f  红线 %.2f  仓位上限 %d%%"
                 % (C.LIGHT_CN[light["light"]], light["index"], light["ma10"] or 0,
                    light["ma20"] or 0, light["red_line"] or 0, light["cap"] * 100))
    if used_days < 3:
        lines.append("主线：存档仅 %d/3 日，判定尚不稳定（继续运行每日收盘复盘积累）"
                     % used_days)
    if mainlines:
        for m in mainlines[:5]:
            lines.append("  · %s [%s] 得分 %d" % (m["sector"], m["level"], m["score"]))
    else:
        lines.append("主线：暂无（需连续 3 日板块存档）")
    lines.append("\n候选池（前 %d 只已写入 候选池.txt，板块列请用业务知识回填）：" % len(pool))
    for i, p in enumerate(pool, 1):
        lines.append("  %2d. %s %s  高开 %.2f%%  %.2f元  流通 %.0f亿  [板块待标注]"
                     % (i, p["code"], p["name"], p["pct"], p["price"], p["nmc_yi"]))
    lines.append("\n龙头候选（③强度按连板数排序；①正宗由你判断）：")
    for i, L in enumerate(leaders, 1):
        star = "★" if L.get("mem") else ("–" if L.get("mem") is False else "?")
        lines.append("  %2d. %s %s  连板 %d  60日涨停 %s 次 %s"
                     % (i, L["code"], L["name"], L.get("lbc") or 0,
                        L.get("zt60"), star))
    lines.append("\n昨日涨停延续度：%s"
                 % ("数据源不可用，需人工判断" if not cont["available"]
                    else " ".join("%s×%d" % (k, v) for k, v in cont["summary"].items())))
    if total_capital:
        lines.append("\n仓位计划：单票上限 %.0f 元（%d%%）"
                     % (plan["max_amount"], plan["single_ratio"] * 100))
    else:
        lines.append("\n⚠️ 未配置账户总资金 → 请向用户询问后执行："
                     "run.py config set account.total_capital=<金额>")
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