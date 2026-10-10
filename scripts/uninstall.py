# -*- coding: utf-8 -*-
"""uninstall.py — a-stock-selection 卸载与「安装前状态」还原。

逆向【安装评估报告】记录的安装动作，把环境还原到安装前：

  1) 回滚安装期对 config.json 的偏离（confirmations.* → 清单原文默认）
  2) 导出技能运行时数据（防丢）→ 回收站
  3) 恢复用户旧系统目录（从安装期备份；备份保留）
  4) 移除安装期新建的镜像数据目录
  5) 移除技能本体目录
  6) 输出定时任务的「删除新任务 / 重建原任务」动作清单（由平台 Agent 执行）

事实源：先发现用户环境中的实际报告「安装评估报告_*.md」（如 安装评估报告_a-stock-selection.md），
解析其字段并与项目基准 docs/cw_setup_info.md 做差异比对，再以**用户实际报告**的取值
（技能目录 / 备份目录 / 镜像目录 / 任务 ID 等）驱动卸载与还原。
参数优先级：命令行显式传入 > 用户实际报告解析值 > 内置默认。

只读扫描模式：--scan 完成上述「发现报告 → 差异比对 → 环境核对」并输出建议还原计划。

安全约束：
  - 默认只做 dry-run（--plan），不改变任何文件；真正执行需 --apply --yes。
  - 绝不硬删除：一律移动到带时间戳的回收站目录（默认 ~/.codebuddy/.trash/<时间戳>/）。
  - 幂等：目标已不存在时跳过并记录，重复执行安全。
  - 自包含：仅用标准库、不 import 项目内模块，技能目录被移除时仍可正常运行。
"""
import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime

# 安装期「待确认规则」被显式改写的目标 → 还原为清单原文默认值（见 scripts/common.py CONFIRM_RULES）。
DEFAULT_CONFIRMATIONS = {"backrow_basis": "price", "mainline_source": "sina"}

# 默认值来自 docs/cw_setup_info.md 的记录；实际环境可用命令行参数覆盖。
DEFAULTS = {
    "skill_dir": "~/.codebuddy/skills/a-stock-selection",
    "backup_dir": "/workspace/交易系统_备份_20261009",
    "restore_to": "/workspace/交易系统",
    "mirror_dir": "/workspace/定盘数据",
    "trash_root": "~/.codebuddy/.trash",
    "new_task_id": "11658801",
    "old_task_id": "11182153",
    "new_task_name": "A股实盘助手",
}

# docs/cw_setup_info.md（【安装评估报告】）描述的安装态——AI 代还原的「扫描比对基准」。
REPORT_REF = {
    "report": "docs/cw_setup_info.md",
    "installed_at": "2026-10-09 20:58",
    "platform": "WorkBuddy",
    "version": "v5",
    "formula_version": "v3 主线龙头低吸版",
    "formula_sha256": "a9950cf9045236953a60cbec50da020d883a1fcd73dfd51d77807a1d886d6b27",
    "installed_confirmations": {"backrow_basis": "pct"},  # 安装期被改写的规则值
}

EXPECT = {
    "skill_files": ["SKILL.md", "VERSION", "scripts/run.py", "docs/定盘实时任务_公式清单.md"],
    "data_files": ["positions.json", "候选池.txt", "history", "最新数据.json"],
    "mirror_files": ["positions.json", "候选池.txt", "config.json", "history", "最新数据.json"],
    "baseline_formula": "/workspace/定盘实时任务_公式清单.md",
}


def expand(p):
    return os.path.abspath(os.path.expanduser(p))


def _ts():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _same(a, b):
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except OSError:
        return False


class Uninstaller(object):
    def __init__(self, args):
        self.args = args
        self.apply = bool(args.apply)
        self.trash = os.path.join(expand(args.trash_root), _ts())
        self.records = []
        self.errors = []
        self._seq = 0

    # -- 记录 -----------------------------------------------------------------
    def log(self, action, target, detail="", status="planned"):
        self._seq += 1
        self.records.append({
            "step": self._seq, "action": action, "target": target,
            "detail": detail, "status": status,
        })
        mark = {"planned": "·", "done": "✓", "skip": "-", "error": "✗"}.get(status, "?")
        line = "  [%s] %s %s" % (mark, action, target)
        if detail:
            line += "  —— %s" % detail
        print(line)

    def fail(self, action, target, reason):
        self.errors.append({"action": action, "target": target, "reason": reason})
        self.log(action, target, reason, status="error")

    # -- 基础操作 -------------------------------------------------------------
    def move_to_trash(self, src, label, step_desc):
        """把 src 整体移入回收站（绝不硬删）；缺失即跳过（幂等）。"""
        src = expand(src)
        if not os.path.exists(src):
            self.log(step_desc, src, "不存在，跳过（可能已卸载）", status="skip")
            return
        dst = os.path.join(self.trash, label)
        if not self.apply:
            self.log(step_desc, src, "将移动到回收站 → %s" % dst)
            return
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.exists(dst):
                dst += "." + _ts()
            shutil.move(src, dst)
            self.log(step_desc, src, "已移动到回收站 → %s" % dst, status="done")
        except Exception as e:  # noqa: BLE001
            self.fail(step_desc, src, "移动失败：%s" % e)

    def write_json(self, path, obj):
        if not self.apply:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            pass

    # -- 步骤 -----------------------------------------------------------------
    def preflight(self):
        print("== 步骤 0｜预检 ==")
        sk = expand(self.args.skill_dir)
        if os.path.exists(sk):
            marker = os.path.join(sk, "SKILL.md")
            if not os.path.exists(marker):
                self.fail("预检技能目录", sk, "缺少 SKILL.md，疑似非本技能目录，已中止")
                return False
            if not os.path.basename(sk.rstrip("/")) == "a-stock-selection":
                print("  ! 技能目录名非 a-stock-selection（%s），请确认 --skill-dir"
                      % os.path.basename(sk.rstrip("/")))
            self.log("预检技能目录", sk, "存在且含 SKILL.md", status="done")
        else:
            self.log("预检技能目录", sk, "不存在（将跳过技能移除）", status="skip")

        bk, to = expand(self.args.backup_dir), expand(self.args.restore_to)
        self._backup_existed = os.path.isdir(bk)
        if os.path.exists(bk):
            self.log("预检备份目录", bk, "存在，可用于还原（还原后保留）", status="done")
        else:
            self.log("预检备份目录", bk, "不存在，无法还原旧系统", status="skip")
        if os.path.exists(to):
            self.log("预检还原目标", to, "已存在，将先移入回收站再还原（--on-exists）",
                     status="done")

        # 备份红线：备份点不得与被操作路径重合 / 被其包含，避免随移除一起被清走。
        for label, p in (("技能目录", sk), ("镜像目录", self.args.mirror_dir),
                         ("还原目标", self.args.restore_to)):
            q = expand(p)
            if _same(bk, q) or bk.startswith(q.rstrip("/") + os.sep):
                self.fail("预检备份红线", bk,
                          "备份点与「%s」重合/被包含，拒绝执行以免误删备份" % label)
                return False
        return True

    def export_runtime_data(self):
        print("== 步骤 2｜导出技能运行时数据（防丢）==")
        out = os.path.join(expand(self.args.skill_dir), "output")
        if os.path.isdir(out):
            self.move_to_trash(out, "skill_output", "导出运行时数据 output/")
        else:
            self.log("导出运行时数据 output/", expand(out), "不存在，跳过", status="skip")

    def revert_config(self):
        print("== 步骤 1｜回滚 config.json 中安装期的规则偏离 ==")
        candidates = []
        for p in (self.args.data_dir or []):
            candidates.append(os.path.join(expand(p), "config.json"))
        candidates.append(os.path.join(expand(self.args.mirror_dir), "config.json"))
        candidates.append(os.path.join(expand(self.args.skill_dir), "output", "config.json"))
        seen = set()
        for cp in candidates:
            rp = os.path.realpath(cp)
            if rp in seen or not os.path.exists(cp):
                continue
            seen.add(rp)
            try:
                with open(cp, encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception as e:  # noqa: BLE001
                self.fail("读取配置", cp, "解析失败：%s" % e)
                continue
            conf = cfg.get("confirmations") or {}
            changed = {k: conf.get(k) for k, v in DEFAULT_CONFIRMATIONS.items()
                       if k in conf and conf.get(k) != v}
            if not changed:
                self.log("回滚规则偏离", cp, "无需回滚", status="skip")
                continue
            if not self.apply:
                self.log("回滚规则偏离", cp,
                         "将还原 %s（当前 %s）"
                         % (", ".join("%s→%s" % (k, DEFAULT_CONFIRMATIONS[k]) for k in changed),
                            changed))
                continue
            for k in changed:
                cfg["confirmations"][k] = DEFAULT_CONFIRMATIONS[k]
            try:
                with open(cp, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                self.log("回滚规则偏离", cp,
                         "已还原 %s" % ", ".join("%s→%s" % (k, DEFAULT_CONFIRMATIONS[k])
                                                 for k in changed), status="done")
            except Exception as e:  # noqa: BLE001
                self.fail("写回配置", cp, "写入失败：%s" % e)

    def restore_old_system(self):
        print("== 步骤 3｜恢复用户旧系统目录 ==")
        bk, to = expand(self.args.backup_dir), expand(self.args.restore_to)
        if not os.path.exists(bk):
            self.log("恢复旧系统", to, "备份目录缺失，跳过", status="skip")
            return
        if os.path.exists(to):
            if not self.args.on_exists == "replace":
                self.log("恢复旧系统", to,
                         "还原目标已存在，跳过（如确认需要覆盖请加 --on-exists replace）",
                         status="skip")
                return
            self.move_to_trash(to, "restore_target_prev", "原目标先移入回收站")
        if not self.apply:
            self.log("恢复旧系统", to, "将从备份 %s 复制还原" % bk)
            return
        try:
            shutil.copytree(bk, to)
            self.log("恢复旧系统", to, "已从备份还原（备份保留于 %s）" % bk, status="done")
        except Exception as e:  # noqa: BLE001
            self.fail("恢复旧系统", to, "还原失败：%s" % e)

    def remove_mirror(self):
        print("== 步骤 4｜移除安装期新建的镜像数据目录 ==")
        self.move_to_trash(self.args.mirror_dir, "mirror_data", "移除镜像数据目录")

    def remove_skill(self):
        print("== 步骤 5｜移除技能本体目录（最后执行）==")
        self.move_to_trash(self.args.skill_dir, "skill_dir", "移除技能本体")

    def schedule_actions(self):
        print("== 步骤 6｜定时任务还原动作（需由平台 Agent 执行）==")
        spec = None
        if self.args.old_task_spec:
            p = expand(self.args.old_task_spec)
            if os.path.exists(p):
                try:
                    with open(p, encoding="utf-8") as f:
                        spec = json.load(f)
                except Exception as e:  # noqa: BLE001
                    self.fail("读取原任务规格", p, "解析失败：%s" % e)
            else:
                self.fail("读取原任务规格", p, "文件不存在")
        actions = {
            "delete_task": {
                "id": self.args.new_task_id,
                "name": self.args.new_task_name,
                "note": "删除安装期新建的定时任务",
            },
            "restore_task": {
                "id": self.args.old_task_id,
                "spec": spec,
                "note": ("按原任务规格重建；未提供 --old-task-spec 时规格未知，"
                         "需用户确认后重建，或从回收站/备份中的任务配置恢复"),
            },
        }
        for k, v in actions.items():
            self.log(k, str(v.get("id")), v["note"])
        return actions

    def verify(self):
        print("== 步骤 7｜校验 ==")
        bk = expand(self.args.backup_dir)
        backup_kept = os.path.isdir(bk) if getattr(self, "_backup_existed", False) else True
        checks = [
            ("技能目录已移除", not os.path.exists(expand(self.args.skill_dir))),
            ("镜像目录已移除", not os.path.exists(expand(self.args.mirror_dir))),
            ("旧系统已还原", os.path.exists(expand(self.args.restore_to))),
            ("原备份点已保留", backup_kept),
        ]
        for name, ok in checks:
            if not self.apply:
                self.log("校验(预演)", name, "将在 --apply 后校验", status="skip")
                continue
            self.log("校验", name, "通过" if ok else "未通过", status="done" if ok else "error")
            if not ok and name not in ("旧系统已还原",):
                self.fail("校验", name, "未通过")

    def run(self):
        print("a-stock-selection 卸载还原  %s  模式=%s\n"
              % (_ts(), "APPLY" if self.apply else "DRY-RUN(默认)"))
        rep_info = getattr(self.args, "_report", {}) or {}
        print("采用用户报告：%s ｜ 基准报告：%s"
              % (rep_info.get("chosen") or "（无，使用内置默认）",
                 rep_info.get("baseline") or "-"))
        if self.apply and not self.args.yes:
            print("✗ 已指定 --apply 但缺少 --yes；为避免误操作已中止。"
                  "确认无误后加 --yes 再执行。")
            return 2
        if not self.preflight():
            return 1
        self.revert_config()
        self.export_runtime_data()
        self.restore_old_system()
        self.remove_mirror()
        self.remove_skill()
        actions = self.schedule_actions()
        self.verify()

        report = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mode": "apply" if self.apply else "plan",
            "params": {
                "skill_dir": expand(self.args.skill_dir),
                "backup_dir": expand(self.args.backup_dir),
                "restore_to": expand(self.args.restore_to),
                "mirror_dir": expand(self.args.mirror_dir),
                "trash": self.trash,
            },
            "install_report": {
                "found": rep_info.get("found") or [],
                "chosen": rep_info.get("chosen"),
                "baseline": rep_info.get("baseline"),
                "diff_vs_baseline": rep_info.get("diff_vs_baseline") or [],
                "effective_params": rep_info.get("effective_params") or {},
            },
            "schedule_actions": actions,
            "backup": {
                "path": expand(self.args.backup_dir),
                "preserved": True,
                "note": "原备份点还原后保留，未删除（--purge 也不涉及备份）",
            },
            "records": self.records,
            "errors": self.errors,
        }
        rpt = os.path.join(self.trash, "uninstall_report.json")
        self.write_json(rpt, report)
        print("\n%s" % ("-" * 56))
        if self.errors:
            print("结果：完成但存在 %d 个问题（见上，疑先处理）" % len(self.errors))
        else:
            print("结果：%s" % ("已执行完成" if self.apply else "预演完成（未改动任何文件）"))
        if self.apply:
            print("回收站：%s\n报告：%s" % (self.trash, rpt))
        else:
            print("提示：确认无误后执行 →  python scripts/uninstall.py --apply --yes")
        if actions["restore_task"]["spec"] is None:
            print("注意：原定时任务 %s 规格未知，需由平台 Agent 按报告重建。"
                  % self.args.old_task_id)
        return 1 if self.errors else 0


# ---------------------------------------------------------------------------
# AI 代还原：环境扫描 + 与安装报告比对
# ---------------------------------------------------------------------------
def _read_text(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _sha256(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _skill_name(skill_md):
    try:
        for line in _read_text(skill_md).splitlines():
            if line.strip().startswith("name:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None


def discover_skill_dirs(roots):
    """在给定根目录下一层内检索 SKILL.md 的 name 为 a-stock-selection 的技能目录。"""
    found = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            d = os.path.join(root, name)
            md = os.path.join(d, "SKILL.md")
            if os.path.isfile(md) and _skill_name(md) == "a-stock-selection":
                found.append(d)
    return found


def _mk_item(items, id_, expected, actual, status, note=""):
    items.append({"id": id_, "expected": expected, "actual": actual,
                  "status": status, "note": note})


def _read_confirmations(cp):
    if not os.path.exists(cp):
        return None
    try:
        with open(cp, encoding="utf-8") as f:
            return (json.load(f).get("confirmations") or {})
    except Exception:  # noqa: BLE001
        return {}


# --- 安装评估报告：发现 / 解析 / 差异比对 -------------------------------------
REPORT_FILE_PREFIX = "安装评估报告"          # 用户环境中的实际报告：安装评估报告_<技能名>.md
REPORT_BASELINE_REL = os.path.join("docs", "cw_setup_info.md")  # 项目内基准报告

# 需要在「用户实际报告 vs 项目基准报告」之间比对的字段
DIFF_FIELDS = ["installed_at", "platform", "skill_dir", "version", "formula_sha256",
               "baseline_formula", "backup_dir", "mirror_dir", "new_task_id",
               "old_task_id", "cron", "task_name", "backrow_basis"]


def find_install_reports(roots):
    """在给定根目录（及其一层子目录）内发现「安装评估报告_*.md」。"""
    out = []
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        out += glob.glob(os.path.join(root, REPORT_FILE_PREFIX + "_*.md"))
        for sub in sorted(glob.glob(os.path.join(root, "*"))):
            if os.path.isdir(sub):
                out += glob.glob(os.path.join(sub, REPORT_FILE_PREFIX + "_*.md"))
    seen, res = set(), []
    for p in out:
        rp = os.path.realpath(p)
        if rp not in seen:
            seen.add(rp)
            res.append(p)
    return res


def _first(pattern, text, group=1, flags=0):
    m = re.search(pattern, text, flags)
    return m.group(group).strip() if m else None


def parse_install_report(text):
    """从【安装评估报告】Markdown 中解析关键事实字段（容错，缺失置 None）。"""
    d = {}
    d["installed_at"] = _first(
        r"安装时间\D{0,8}([0-9]{4}-[0-9]{2}-[0-9]{2}(?:\s+[0-9]{1,2}:[0-9]{2})?)", text)
    d["platform"] = _first(r"目标平台[^A-Za-z0-9\u4e00-\u9fa5]{0,8}([A-Za-z0-9\u4e00-\u9fa5]+)", text)
    d["skill_dir"] = _first(r"(~?/[^\s`|]*/a-stock-selection)", text)
    d["version"] = (_first(r"VERSION`?\s*\|?\s*(?:✅\s*)?(v[0-9]+)", text)
                    or _first(r"version\s+(v[0-9]+)", text)
                    or _first(r"\b(v[0-9]+)\b", text))
    d["formula_sha256"] = (_first(r"sha256\s+([0-9a-f]{64})", text)
                           or _first(r"`([0-9a-f]{64})`", text))
    d["baseline_formula"] = _first(r"([^\s`|]*定盘实时任务_公式清单\.md)", text)
    d["backup_dir"] = (_first(r"已备份\**\s*[:：]\s*`([^`]+)`", text)
                       or _first(r"(/[^\s`]*_备份_[0-9]{6,}[^`\s)）]*)", text))
    d["mirror_dir"] = _first(r"`(/[^\s`]*定盘数据/?)[^`]*`", text)
    d["new_task_id"] = _first(r"任务\s*ID[^\d]{0,14}(\d{6,})", text)
    d["old_task_id"] = _first(r"原任务\s*#?(\d{6,})", text)
    d["cron"] = _first(r"Cron[^`\n]{0,6}`([^`]+)`", text)
    d["task_name"] = _first(r"^\|\s*名称\s*\|\s*([^|\n]+?)\s*\|", text, flags=re.M)
    d["backrow_basis"] = _first(r"backrow_basis\s*=\s*([A-Za-z]+)", text)
    d["data_files"] = [f for f in EXPECT["data_files"] if f in text]
    return d


def diff_install_reports(actual, baseline):
    """逐字段比对「用户实际报告」与「项目基准报告」。"""
    res = []
    for f in DIFF_FIELDS:
        a, b = actual.get(f), baseline.get(f)
        if a is None and b is None:
            status = "unknown"
        elif a == b:
            status = "equal"
        elif a is None or b is None:
            status = "partial"
        else:
            status = "different"
        res.append({"field": f, "actual": a, "baseline": b, "status": status})
    return res


def _infer_restore_to(backup_dir):
    """从备份目录名反推还原目标：/…/X_备份_20261009/ → /…/X。"""
    if not backup_dir:
        return None
    p = backup_dir.rstrip("/")
    m = re.match(r"^(.*?)_备份_[0-9]{6,}$", os.path.basename(p))
    if m and m.group(1):
        return os.path.join(os.path.dirname(p), m.group(1))
    return None


def resolve_params(args):
    """发现用户实际【安装评估报告】→ 解析 → 与项目基准比对 → 以其取值驱动还原。

    参数优先级：命令行显式传入 > 用户实际报告解析值 > 内置默认（DEFAULTS）。
    """
    proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    baseline_path = os.path.join(proj, REPORT_BASELINE_REL)
    report_roots = [expand(r) for r in (args.report_root or [])] or [
        os.getcwd(), proj, os.path.join(proj, "docs"),
        os.path.dirname(expand(args.skill_dir or DEFAULTS["skill_dir"])),
        "/workspace",
    ]
    found = find_install_reports(report_roots)
    chosen = expand(args.report) if args.report else (found[0] if found else None)
    actual = parse_install_report(_read_text(chosen)) if (chosen and os.path.exists(chosen)) else {}
    baseline = parse_install_report(_read_text(baseline_path)) if os.path.exists(baseline_path) else {}
    diff = diff_install_reports(actual, baseline)

    eff = {
        "skill_dir": actual.get("skill_dir"),
        "backup_dir": actual.get("backup_dir"),
        "mirror_dir": actual.get("mirror_dir"),
        "new_task_id": actual.get("new_task_id"),
        "old_task_id": actual.get("old_task_id"),
        "new_task_name": actual.get("task_name"),
        "restore_to": _infer_restore_to(actual.get("backup_dir")),
    }
    # 命令行显式值 > 报告解析值 > 内置默认
    args.skill_dir = args.skill_dir or eff["skill_dir"] or DEFAULTS["skill_dir"]
    args.backup_dir = args.backup_dir or eff["backup_dir"] or DEFAULTS["backup_dir"]
    args.mirror_dir = args.mirror_dir or eff["mirror_dir"] or DEFAULTS["mirror_dir"]
    args.restore_to = args.restore_to or eff["restore_to"] or DEFAULTS["restore_to"]
    args.new_task_id = args.new_task_id or eff["new_task_id"] or DEFAULTS["new_task_id"]
    args.old_task_id = args.old_task_id or eff["old_task_id"] or DEFAULTS["old_task_id"]
    args.new_task_name = args.new_task_name or eff["new_task_name"] or DEFAULTS["new_task_name"]

    args._report = {
        "found": found,
        "chosen": chosen,
        "baseline": baseline_path,
        "actual": actual,
        "baseline_parsed": baseline,
        "diff_vs_baseline": diff,
        "effective_params": {k: v for k, v in eff.items() if v},
    }
    return args


def scan_environment(args):
    """扫描用户环境：发现用户实际【安装评估报告】并与项目基准比对；
    再以「用户实际报告」为事实源，逐项核对环境现状；只读，不改动。"""
    skill_dir = expand(args.skill_dir)
    backup_dir = expand(args.backup_dir)
    restore_to = expand(args.restore_to)
    mirror_dir = expand(args.mirror_dir)
    roots = [expand(r) for r in (args.scan_root or [])] or [os.path.dirname(skill_dir)]

    # 以「用户实际安装评估报告」解析值为事实源；缺失字段回退项目基准（REPORT_REF/EXPECT）。
    rep_info = getattr(args, "_report", {}) or {}
    actual = rep_info.get("actual") or {}
    ref_version = actual.get("version") or REPORT_REF["version"]
    ref_sha = actual.get("formula_sha256") or REPORT_REF["formula_sha256"]
    baseline_formula = actual.get("baseline_formula") or EXPECT["baseline_formula"]
    data_files = actual.get("data_files") or EXPECT["data_files"]
    src = ("用户实际报告 %s" % rep_info.get("chosen")) if rep_info.get("chosen") else "项目基准（未发现用户报告）"

    items = []
    found = discover_skill_dirs(roots)
    base = skill_dir if os.path.isdir(skill_dir) else (found[0] if found else None)

    # 技能本体
    if os.path.isdir(skill_dir):
        _mk_item(items, "skill_dir", "存在：%s（%s）" % (skill_dir, src), "存在", "match")
    elif found:
        _mk_item(items, "skill_dir", "存在：%s（%s）" % (skill_dir, src),
                 "默认路径缺失，但在 %s 发现同名技能" % "、".join(found),
                 "mismatch", "位置与报告不同，请用 --skill-dir 指定实际路径")
    else:
        _mk_item(items, "skill_dir", "存在：%s（%s）" % (skill_dir, src),
                 "缺失", "missing", "已卸载或未安装")

    if base:
        for rel in EXPECT["skill_files"]:
            p = os.path.join(base, rel)
            _mk_item(items, "skill_file:%s" % rel, "存在",
                     "存在" if os.path.exists(p) else "缺失",
                     "match" if os.path.exists(p) else "missing")
        vpath = os.path.join(base, "VERSION")
        if os.path.exists(vpath):
            v = _read_text(vpath).strip()
            _mk_item(items, "version", ref_version, v,
                     "match" if v == ref_version else "mismatch",
                     "" if v == ref_version else "与报告版本不符")
        fp = os.path.join(base, "docs", "定盘实时任务_公式清单.md")
        if os.path.exists(fp):
            h = _sha256(fp)
            _mk_item(items, "formula_sha256", ref_sha[:12] + "…",
                     (h or "")[:12] + "…",
                     "match" if h == ref_sha else "mismatch")
        for rel in data_files:
            p = os.path.join(base, "output", rel)
            _mk_item(items, "data:%s" % rel, "存在（报告迁移记录）",
                     "存在" if os.path.exists(p) else "缺失",
                     "match" if os.path.exists(p) else "missing")

    # 镜像目录 / 备份点 / 还原目标
    if os.path.isdir(mirror_dir):
        _mk_item(items, "mirror_dir", "存在：%s（报告第七节）" % mirror_dir, "存在", "match")
        for rel in EXPECT["mirror_files"]:
            p = os.path.join(mirror_dir, rel)
            _mk_item(items, "mirror:%s" % rel, "存在",
                     "存在" if os.path.exists(p) else "缺失",
                     "match" if os.path.exists(p) else "missing")
    else:
        _mk_item(items, "mirror_dir", "存在：%s（报告第七节）" % mirror_dir, "缺失", "missing")

    _mk_item(items, "backup_dir", "存在：%s（还原后须保留）" % backup_dir,
             "存在" if os.path.isdir(backup_dir) else "缺失",
             "match" if os.path.isdir(backup_dir) else "missing",
             "还原时此目录不得删除" if os.path.isdir(backup_dir) else "")
    _mk_item(items, "restore_to", "缺省（原件已迁至备份）", 
             "存在" if os.path.exists(restore_to) else "缺失",
             "present" if os.path.exists(restore_to) else "match",
             "还原目标已存在，还原前需 --on-exists 决策")

    bp = expand(baseline_formula)
    if os.path.exists(bp):
        hb = _sha256(bp)
        _mk_item(items, "baseline_formula", "存在且与项目清单 sha 同源",
                 "存在 %s…" % (hb or "")[:12],
                 "match" if hb == ref_sha else "mismatch")
    else:
        _mk_item(items, "baseline_formula", "存在（报告环境基线）", "缺失", "missing")

    # 规则偏离（安装期被改写的 confirmations）
    configs = []
    for cp in ([os.path.join(expand(p), "config.json") for p in (args.data_dir or [])]
               + [os.path.join(mirror_dir, "config.json"),
                  os.path.join(base, "output", "config.json") if base else ""]):
        if not cp or not os.path.exists(cp):
            continue
        conf = _read_confirmations(cp) or {}
        delta = {k: conf.get(k) for k, v in DEFAULT_CONFIRMATIONS.items()
                 if k in conf and conf.get(k) != v}
        configs.append({"path": cp, "confirmations": conf, "to_rollback": delta})

    deviations = [it for it in items if it["status"] in ("mismatch", "missing")]
    installed = base is not None
    restore_needed = bool(installed or os.path.isdir(mirror_dir) or os.path.exists(restore_to))
    plan = []
    if installed:
        plan.append("回滚 config.json 偏离 → 导出运行时数据 → 移除技能本体 %s" % (base or skill_dir))
    if os.path.isdir(backup_dir):
        plan.append("从备份还原旧系统 → %s（备份 %s 保留）" % (restore_to, backup_dir))
    else:
        plan.append("⚠️ 备份缺失，无法还原旧系统；仅能卸载技能与清理镜像")
    if os.path.isdir(mirror_dir):
        plan.append("移除镜像目录 %s" % mirror_dir)
    plan.append("定时任务：删除新任务 %s；按规格重建原任务 %s"
                % (args.new_task_id, args.old_task_id))

    return {
        "reference": {
            "report_path": rep_info.get("chosen") or REPORT_REF["report"],
            "baseline_path": rep_info.get("baseline"),
            "installed_at": actual.get("installed_at") or REPORT_REF["installed_at"],
            "platform": actual.get("platform") or REPORT_REF["platform"],
            "version": ref_version,
            "formula_sha256": ref_sha,
        },
        "install_report": {
            "found": rep_info.get("found") or [],
            "chosen": rep_info.get("chosen"),
            "baseline": rep_info.get("baseline"),
            "parsed": actual,
            "diff_vs_baseline": rep_info.get("diff_vs_baseline") or [],
        },
        "effective_params": rep_info.get("effective_params") or {},
        "scanned_roots": roots,
        "skill_dir_resolved": base,
        "items": items,
        "configs": configs,
        "verdict": {
            "installed": installed,
            "restore_needed": restore_needed,
            "backup_present": os.path.isdir(backup_dir),
            "deviations": [it["id"] for it in deviations],
        },
        "recommended_plan": plan,
    }


_MARK = {"match": "✓", "missing": "✗", "mismatch": "≠", "present": "+", "skip": "-"}


def print_scan(rep):
    print("== AI 代还原 · 环境扫描（比对安装评估报告）==")
    ir = rep.get("install_report") or {}
    found = ir.get("found") or []
    print("  发现用户报告：%s" % ("、".join(found) if found else "无（回退项目基准）"))
    print("  采用报告：%s" % (ir.get("chosen") or "（无，使用 docs/cw_setup_info.md 基准）"))
    print("  基准报告：%s" % (ir.get("baseline") or "-"))
    print("  安装态：%s @ %s ｜ 平台 %s ｜ 版本 %s"
          % (rep["reference"]["report_path"], rep["reference"]["installed_at"],
             rep["reference"]["platform"], rep["reference"]["version"]))
    diff = ir.get("diff_vs_baseline") or []
    if diff and ir.get("chosen"):
        print("  -- 用户实际报告 vs 项目基准（docs/cw_setup_info.md）差异 --")
        shown = False
        for row in diff:
            if row["status"] == "equal":
                continue
            shown = True
            print("     [%s] %-16s 实际：%s  ｜ 基准：%s"
                  % ("≠" if row["status"] == "different" else
                     ("?" if row["status"] == "partial" else "·"),
                     row["field"], row["actual"], row["baseline"]))
        if not shown:
            print("     （无差异：用户实际报告与项目基准一致）")
    eff = rep.get("effective_params") or {}
    if eff:
        print("  -- 以用户实际报告驱动还原的参数 --")
        for k, v in eff.items():
            print("     %s = %s" % (k, v))
    print("  扫描根：%s" % "、".join(rep["scanned_roots"]))
    for it in rep["items"]:
        line = "  [%s] %-28s 实测：%s" % (_MARK.get(it["status"], "?"),
                                          it["id"], it["actual"])
        if it["status"] != "match" and it["note"]:
            line += "  —— %s" % it["note"]
        print(line)
    for c in rep["configs"]:
        if c["to_rollback"]:
            print("  [≠] 配置偏离 %-22s %s（需回滚为 %s）"
                  % (c["path"], c["to_rollback"], DEFAULT_CONFIRMATIONS))
    v = rep["verdict"]
    print("  ---- 判定：installed=%s  restore_needed=%s  backup_present=%s  deviations=%s"
          % (v["installed"], v["restore_needed"], v["backup_present"],
             "、".join(v["deviations"]) or "无"))
    print("  建议还原计划：")
    for i, s in enumerate(rep["recommended_plan"], 1):
        print("    %d) %s" % (i, s))
    print("  提示：备份点始终保留；确认无误后执行 → python scripts/uninstall.py --apply --yes")


def build_parser():
    d = DEFAULTS
    p = argparse.ArgumentParser(
        prog="uninstall.py",
        description="a-stock-selection 卸载与安装前状态还原（默认 dry-run）")
    p.add_argument("--plan", action="store_true", help="仅预演，不改动文件（默认行为）")
    p.add_argument("--scan", action="store_true",
                   help="仅扫描环境、发现用户【安装评估报告】并与 docs/cw_setup_info.md 比对（只读）")
    p.add_argument("--scan-root", action="append",
                   help="技能检索根目录（可多次；默认取 --skill-dir 的父目录）")
    p.add_argument("--report", help="用户实际的【安装评估报告_*.md】路径（默认自动发现）")
    p.add_argument("--report-root", action="append",
                   help="安装评估报告的检索根目录（可多次；默认 cwd/项目/docs/技能父目录/workspace）")
    p.add_argument("--apply", action="store_true", help="真正执行（默认仅预演）")
    p.add_argument("--json", action="store_true", help="结构化 JSON 输出（配合 --scan）")
    p.add_argument("--yes", action="store_true", help="与 --apply 连用，确认执行")
    p.add_argument("--skill-dir", default=None, help="技能本体目录（缺省取实际报告解析值）")
    p.add_argument("--backup-dir", default=None, help="安装期备份目录（缺省取实际报告解析值）")
    p.add_argument("--restore-to", default=None,
                   help="还原旧系统的目标目录（缺省由备份目录名 _备份_ 反推）")
    p.add_argument("--mirror-dir", default=None, help="镜像数据目录（缺省取实际报告解析值）")
    p.add_argument("--trash-root", default=d["trash_root"], help="回收站根目录")
    p.add_argument("--data-dir", action="append", help="额外扫描 config.json 的数据目录（可多次）")
    p.add_argument("--on-exists", choices=["skip", "replace"], default="skip",
                   help="还原目标已存在时：skip=中止(默认) / replace=先入回收站再还原")
    p.add_argument("--new-task-id", default=None, help="要删除的新任务 ID（缺省取实际报告解析值）")
    p.add_argument("--new-task-name", default=None, help="新任务名称（缺省取实际报告解析值）")
    p.add_argument("--old-task-id", default=None, help="要重建的原任务 ID（缺省取实际报告解析值）")
    p.add_argument("--old-task-spec", help="原任务规格 JSON 文件（含 cron/时区/入口等）")
    p.add_argument("--purge", action="store_true",
                   help="执行后永久删除回收站（不含原备份点；危险，默认关闭）")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.plan and args.apply:
        print("✗ --plan 与 --apply 互斥，请二选一。")
        return 2
    # 发现用户实际【安装评估报告】→ 与项目基准比对 → 以报告取值驱动还原
    resolve_params(args)
    if args.scan:
        rep = scan_environment(args)
        if args.json:
            print(json.dumps(rep, ensure_ascii=False, indent=2))
        else:
            print_scan(rep)
        return 0
    runner = Uninstaller(args)
    rc = runner.run()
    if args.purge and args.apply and os.path.isdir(runner.trash):
        shutil.rmtree(runner.trash, ignore_errors=True)
        print("已永久删除回收站：%s" % runner.trash)
    return rc


if __name__ == "__main__":
    sys.exit(main())