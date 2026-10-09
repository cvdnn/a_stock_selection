# -*- coding: utf-8 -*-
"""run.py — 定盘实时任务统一入口。

每 10 分钟执行 `python run.py auto`，脚本按交易时段自动分流；
另提供 config / positions / pool / selfcheck 子命令，供 Agent 收集用户参数。
"""
import os
import sys
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402


def _global_flags(args):
    if getattr(args, "data_dir", None):
        C.set_data_dir(args.data_dir)
    if getattr(args, "offline", False):
        C.set_offline(True)


def _stage_main(name, args):
    mod = __import__(name)
    argv = []
    if getattr(args, "data_dir", None):
        argv += ["--data-dir", args.data_dir]
    if getattr(args, "offline", False):
        argv += ["--offline"]
    if getattr(args, "json", False):
        argv += ["--json"]
    return mod.main(argv)


def _calendar_hint(args):
    """临期/到期时在文本输出末尾追加刷新指引（--json 不追加，结构化字段已含）。"""
    if getattr(args, "json", False):
        return
    hint = C.calendar_refresh_hint()
    if hint:
        C.out("\n⚠️ " + hint)


def cmd_auto(args):
    _global_flags(args)
    stage = args.stage or C.current_stage(cfg=C.load_config())
    if stage == "closed":
        C.out("休市：不运行脚本。")
        _calendar_hint(args)
        return 0
    if stage == "freeze":
        C.out("数据定格中（15:00-15:35），15:35 后运行收盘复盘。")
        _calendar_hint(args)
        return 0
    rc = _stage_main(stage, args)
    _calendar_hint(args)
    return rc


def cmd_config(args):
    _global_flags(args)
    if args.action == "init":
        cfg = C.load_config()
        cur = cfg["account"].get("total_capital") or 0
        val = input("账户总资金（元）[当前 %s]：" % cur).strip()
        if val:
            cfg["account"]["total_capital"] = float(val)
        mode = input("数据源模式 live/offline [当前 %s]：" % cfg["datasource"]["mode"]).strip()
        if mode in ("live", "offline"):
            cfg["datasource"]["mode"] = mode
        C.save_config(cfg)
        C.out("已保存到 %s" % C.config_path())
    elif args.action == "set":
        if "=" not in args.pair:
            raise SystemExit("用法：config set key=value，例如 account.total_capital=200000")
        k, v = args.pair.split("=", 1)
        C.set_config_value(k.strip(), v)
        C.out("已设置 %s = %s" % (k.strip(), v))
    else:  # show
        cfg = C.load_config()
        C.out("数据目录：%s\n配置：%s" % (C.get_data_dir(), C.config_path()),
              as_json=cfg if args.json else None)
    return 0


def cmd_positions(args):
    _global_flags(args)
    if args.action == "add":
        code = None
        kv = {}
        for t in (args.spec or []):
            if "=" in t:
                k, v = t.split("=", 1)
                kv[k.strip().lower()] = v.strip()
            elif code is None:
                code = t
        code = code or kv.pop("code", None)
        if not code:
            code = input("股票代码：").strip()
        cost = kv.pop("cost", None)
        if cost in (None, ""):
            cost = input("成本价：").strip()
        stop = kv.pop("stop", None)
        if stop in (None, ""):
            stop = input("自设止损价（可留空）：").strip()
        target = kv.pop("target", None)
        if target in (None, ""):
            target = input("目标价（可留空）：").strip()
        shares = kv.pop("shares", None)
        if shares in (None, ""):
            shares = input("股数（可留空）：").strip()
        items = C.add_position(code, cost, stop, target, shares)
        C.out("已保存持仓，当前 %d 条" % len(items))
    elif args.action == "remove":
        code = (args.spec or [None])[0] or input("要删除的股票代码：").strip()
        items = C.remove_position(code)
        C.out("已删除，当前 %d 条" % len(items))
    elif args.action == "clear":
        C.save_positions([])
        C.out("已清空持仓")
    else:  # list
        items = C.load_positions()
        if args.json:
            C.out("", as_json={"positions": items})
        elif not items:
            C.out("无持仓。录入：positions add <代码> cost=<成本> [stop=] [target=] [shares=]")
        else:
            for p in items:
                C.out("  %s %s  成本 %s  止损 %s  目标 %s  股数 %s"
                      % (p["code"], p.get("name", ""), p.get("cost"),
                         p.get("stop"), p.get("target"), p.get("shares")))
    return 0


def cmd_pool(args):
    _global_flags(args)
    rows = C.load_pool()
    if getattr(args, "action", "show") == "set":
        # 回填板块列：run.py pool set <代码> <板块>
        spec = args.spec or []
        if len(spec) < 2:
            raise SystemExit("用法：pool set <代码> <板块>")
        code = C.normalize_code(spec[0])
        sector = spec[1]
        hit = False
        for r in rows:
            if r["code"] == code:
                r["sector"] = sector
                hit = True
        if not hit:
            C.out("候选池中无 %s" % code)
            return 1
        C.save_pool(rows)
        C.out("已回填 %s → %s" % (code, sector))
        return 0
    if args.json:
        C.out("", as_json={"pool": rows})
    elif not rows:
        C.out("候选池为空。运行 preopen 生成：run.py auto --stage preopen")
    else:
        for r in rows:
            C.out("  %s %s  %.2f%%  %.2f元  %.0f亿  [%s]"
                  % (r["code"], r["name"], r["pct"], r["price"], r["nmc_yi"],
                     r["sector"] or "板块待标注"))
    return 0


def cmd_calendar(args):
    _global_flags(args)
    if getattr(args, "action", "show") == "set":
        cfg = C.load_config()
        cal = cfg.setdefault("calendar", {})
        kvs = {}
        for t in (args.spec or []):
            if "=" not in t:
                raise SystemExit("用法：calendar set holidays='[\"2026-01-01\",...]' "
                                 "coverage_to=2027-12-31 source=<来源>")
            k, v = t.split("=", 1)
            kvs[k.strip()] = v.strip()
        for k, v in kvs.items():
            cal[k] = C._coerce(v)
        if "holidays" in kvs:
            cal["holidays"] = sorted(set(cal.get("holidays") or []))
        cal["generated_at"] = datetime.now().strftime("%Y-%m-%d")
        C.save_config(cfg)
        C.out("已更新交易日历：覆盖至 %s  休市日 %d 个  来源 %s"
              % (cal.get("coverage_to"), len(cal.get("holidays") or []), cal.get("source")))
        return 0
    st = C.calendar_status()
    left = st["days_left"] if st["days_left"] is not None else "-"
    C.out("交易日历：覆盖至 %s  休市日 %d 个  生成 %s  来源 %s  距到期 %s 天"
          % (st["coverage_to"] or "无", st["holidays"], st["generated_at"] or "-",
             st["source"] or "-", left),
          as_json=st if args.json else None)
    hint = C.calendar_refresh_hint()
    if hint:
        C.out("⚠️ " + hint)
    return 0


def cmd_selfcheck(args):
    _global_flags(args)
    cfg = C.load_config()
    formula = C.formula_list_check()
    baseline = getattr(args, "baseline", None)
    diff = C.formula_list_diff(baseline) if baseline else None

    doc_ok = formula["exists"] and formula["readable"]
    if not doc_ok:
        reason = "formula_list_missing_or_unreadable"
    elif diff is not None and not diff["consistent"]:
        reason = "baseline_mismatch"
    else:
        reason = None

    info = {
        "python": sys.version.split()[0],
        "skill_root": C.SKILL_ROOT,
        "data_dir": C.get_data_dir(),
        "config_path": C.config_path(),
        "pool_path": C.pool_path(),
        "positions_path": C.positions_path(),
        "archive_path": C.archive_path(),
        "offline": C.is_offline(),
        "stage_now": C.current_stage(),
        "total_capital": cfg["account"].get("total_capital"),
        "datasource_mode": cfg["datasource"]["mode"],
        "calendar": C.calendar_status(cfg),
        "formula_list": formula,
        "verify": {"ok": reason is None, "reason": reason},
    }
    if diff is not None:
        info["formula_list_diff"] = diff

    C.out("自检:", as_json=info if args.json else None)
    if not args.json:
        for k, v in info.items():
            if k in ("formula_list", "formula_list_diff", "verify"):
                continue
            C.out("  %-16s %s" % (k, v))
        C.out("  公式清单         %s" % ("存在" if formula["exists"] else "缺失"))
        C.out("    path           %s" % formula["path"])
        C.out("    version        %s" % (formula["version"] or "-"))
        C.out("    lines          %s" % formula["lines"])
        C.out("    sha256         %s" % (formula["sha256"] or "-"))
        C.out("    sections       %d 节" % len(formula["sections"]))
        if diff is not None:
            C.out("  清单比对(基线 %s):" % baseline)
            C.out("    版本(基线/项目) %s / %s"
                  % (diff["baseline_version"] or "-", diff["project_version"] or "-"))
            C.out("    仅环境有(项目缺) %s" % ("、".join(diff["removed_sections"]) or "无"))
            C.out("    仅项目有        %s" % ("、".join(diff["added_sections"]) or "无"))
            C.out("    取值不同        %s" % ("、".join(diff["changed_sections"]) or "无"))
            C.out("    结论            %s" % ("一致" if diff["consistent"] else "不一致"))
        # 安装后自动校验结论（文本模式；--json 由 verify 字段透出）
        if reason == "formula_list_missing_or_unreadable":
            C.out("\n✗ 公式清单缺失或不可读：%s\n  安装不完整，请重新复制完整项目（含 docs/）。"
                  % formula["path"])
        elif reason == "baseline_mismatch":
            C.out("\n✗ 清单与安装环境基线不一致，当前项目可能不满足用户选股需求。\n"
                  "  请把差异反馈给管理员或开发者，升级项目后再安装。")
        else:
            C.out("\n✓ 安装后校验通过。")
    return 0 if reason is None else 1


def cmd_cache(args):
    _global_flags(args)
    if args.action == "clear":
        n = C.cache_clear()
        C.out("已清空数据缓存：删除 %d 个文件" % n)
        return 0
    st = C.cache_status()
    C.out("数据缓存：%s  全局兜底 TTL %ss  目录 %s  条目 %d"
          % ("启用" if st["enabled"] else "关闭", st["ttl_seconds"], st["dir"], st["count"]),
          as_json=st if args.json else None)
    if not args.json:
        for it in st["items"][:30]:
            C.out("  %-14s %-28s TTL %3ss  龄 %4.0fs  %s"
                  % (it["source"], it["key"], it["ttl"], it["age"], it["time"]))
        if not st["enabled"]:
            C.out("  提示：缓存默认关闭。启用：run.py config set datasource.cache.enabled=true")
        C.out("  按数据源覆盖 TTL：run.py config set datasource.cache.ttl='{\"daily\":600}'")
    return 0


def cmd_sample(args):
    _global_flags(args)
    if args.action == "gen":
        codes = [x.strip() for x in (args.codes or "").split(",") if x.strip()]
        info = C.sample_gen(codes or None, getattr(args, "date", None))
        C.out("已生成离线样例：%d 只股票  日期 %s  涨停池 %d 只\n  写入 %s"
              % (len(info["codes"]), info["date"], info["limit_up_count"], C.SAMPLE_DIR),
              as_json=info if args.json else None)
        return 0
    rep = C.sample_verify()
    if args.json:
        C.out("", as_json=rep)
        return 0
    C.out("离线样例校验：%s" % ("通过 ✓" if rep["ok"] else "未通过 ✗"))
    for name, f in rep["files"].items():
        C.out("  %-18s %3d 条  %s" % (name, f["count"],
              "OK" if not f["issues"] else "问题：" + "；".join(f["issues"])))
    if rep["consistency_issues"]:
        C.out("  跨文件一致性：")
        for x in rep["consistency_issues"]:
            C.out("    ✗ %s" % x)
    else:
        C.out("  跨文件一致性：OK")
    return 0 if rep["ok"] else 1


def build_parser():
    g = argparse.ArgumentParser(prog="run.py", description="A股定盘实时任务入口")
    g.add_argument("--data-dir", help="运行数据目录（默认项目内 output/）")
    g.add_argument("--offline", action="store_true", help="使用 assets/sample 离线样例")
    g.add_argument("--json", action="store_true", help="结构化 JSON 输出")
    sub = g.add_subparsers(dest="cmd")

    p_auto = sub.add_parser("auto", help="按时段自动分流（定时任务用）")
    p_auto.add_argument("--stage", choices=["preopen", "watch", "daily"], help="强制指定阶段")
    p_auto.set_defaults(func=cmd_auto)

    for name in ("preopen", "watch", "daily"):
        p = sub.add_parser(name, help="直接运行 %s 内核" % name)
        p.set_defaults(func=(lambda n: (lambda a: _stage_main(n, a)))(name))

    p_cfg = sub.add_parser("config", help="配置")
    p_cfg.add_argument("action", nargs="?", default="show", choices=["show", "set", "init"])
    p_cfg.add_argument("pair", nargs="?", help="key=value（set 用）")
    p_cfg.set_defaults(func=cmd_config)

    p_pos = sub.add_parser("positions", help="持仓")
    p_pos.add_argument("action", nargs="?", default="list", choices=["list", "add", "remove", "clear"])
    p_pos.add_argument("spec", nargs="*",
                       help="add 例：600519 cost=12.50 stop=11.80 target=15.00 shares=1000；remove 例：600519")
    p_pos.set_defaults(func=cmd_positions)

    p_pool = sub.add_parser("pool", help="查看/回填候选池")
    p_pool.add_argument("action", nargs="?", default="show", choices=["show", "set"])
    p_pool.add_argument("spec", nargs="*", help="set 例：pool set 600000 银行")
    p_pool.set_defaults(func=cmd_pool)

    p_cal = sub.add_parser("calendar", help="交易日历（查看/更新）")
    p_cal.add_argument("action", nargs="?", default="show", choices=["show", "set"])
    p_cal.add_argument("spec", nargs="*",
                       help="set 例：holidays='[\"2026-01-01\"]' coverage_to=2027-12-31 source=上交所")
    p_cal.set_defaults(func=cmd_calendar)

    p_sc = sub.add_parser("selfcheck", help="环境自检（含公式清单校验）")
    p_sc.add_argument("--baseline", help="安装环境已有公式清单路径，用于一致性比对")
    p_sc.set_defaults(func=cmd_selfcheck)

    p_cache = sub.add_parser("cache", help="数据缓存（查看/清空）")
    p_cache.add_argument("action", nargs="?", default="show", choices=["show", "clear"])
    p_cache.set_defaults(func=cmd_cache)

    p_sample = sub.add_parser("sample", help="离线样例（校验/生成）")
    p_sample.add_argument("action", nargs="?", default="verify", choices=["verify", "gen"])
    p_sample.add_argument("--codes", help="gen：逗号分隔代码，如 600000,000001")
    p_sample.add_argument("--date", help="gen：数据日期 YYYY-MM-DD（默认今天）")
    p_sample.set_defaults(func=cmd_sample)
    return g


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # 全局开关允许出现在任意位置（含子命令之后），先摘除再交给 argparse。
    data_dir, offline, json_out, rest = None, False, False, []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--data-dir" and i + 1 < len(argv):
            data_dir = argv[i + 1]
            i += 2
            continue
        if a.startswith("--data-dir="):
            data_dir = a.split("=", 1)[1]
            i += 1
            continue
        if a == "--offline":
            offline = True
            i += 1
            continue
        if a == "--json":
            json_out = True
            i += 1
            continue
        rest.append(a)
        i += 1
    if data_dir:
        C.set_data_dir(data_dir)
    if offline:
        C.set_offline(True)

    parser = build_parser()
    args = parser.parse_args(rest)
    args.data_dir = data_dir
    args.offline = offline
    args.json = json_out
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())