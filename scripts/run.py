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


def cmd_auto(args):
    _global_flags(args)
    stage = args.stage or C.current_stage(cfg=C.load_config())
    if stage == "closed":
        C.out("休市：不运行脚本。")
        return 0
    if stage == "freeze":
        C.out("数据定格中（15:00-15:35），15:35 后运行收盘复盘。")
        return 0
    return _stage_main(stage, args)


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


def cmd_selfcheck(args):
    _global_flags(args)
    cfg = C.load_config()
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
    }
    C.out("自检:", as_json=info if args.json else None)
    if not args.json:
        for k, v in info.items():
            C.out("  %-16s %s" % (k, v))
    return 0


def build_parser():
    g = argparse.ArgumentParser(prog="run.py", description="A股定盘实时任务入口")
    g.add_argument("--data-dir", help="运行数据目录（默认 ./dingpan_data）")
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

    p_sc = sub.add_parser("selfcheck", help="环境自检")
    p_sc.set_defaults(func=cmd_selfcheck)
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