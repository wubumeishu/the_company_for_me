"""
sop_contract.py —— Phase 3b-③：dev_handoff 强契约 + SOP Gate
（PHASE_3_ENGINEERING_DEFENSE §1.1 MetaGPT SOP 交接契约 + §7 拍板 #4「强契约」）。

主人拍板 #4：dev_handoff 是**强契约**——Dev 节点交出的产物里必须带一份**格式合法**的
交付说明（dev_handoff），缺字段 / 格式错 = SOP Gate 直接让节点 FAIL，**不触发 git 提交**，
强迫按规范交接（呼应「build 成功 ≠ QA 过」红线：口头/残缺交接不算完成）。

契约（dev_handoff 必含字段，缺一即 FAIL）：
  - commit_message   规范化提交说明（非空）
  - self_check       Dev 自查声明（dict，至少 ran[] / passed）
  - pr_description   给 QA 的 PR 说明（非空，QA checklist 依据）
  - changed_files    产物→目标文件映射（list，可空；给了必须 list）

载体：Dev 节点 outputs 里的 "dev_handoff" 键，值 = JSON 字符串或 dict。
SOP Gate（validate_dev_handoff）= 纯函数，单测全分支可离线跑（零 git 零进程）。
"""
from __future__ import annotations

import json
from typing import Any

# 强契约必填字段（缺一即 SOP Gate FAIL，拍板 #4）
REQUIRED_FIELDS = ("commit_message", "self_check", "pr_description")
# self_check 里应声明的最小项（缺 = 自查不完整，也 FAIL）
REQUIRED_SELF_CHECK_KEYS = ("ran", "passed")


def parse_handoff(raw: Any) -> tuple[bool, str, dict[str, Any]]:
    """把 outputs["dev_handoff"] 解析成 dict。返回 (ok, 原因, 解析结果)。
    载体可为 dict（引擎内部）或 JSON 字符串（Dev 产物常是文本）。坏 JSON = 格式不合法。"""
    if isinstance(raw, dict):
        return True, "", raw
    if isinstance(raw, str):
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as e:
            return False, f"dev_handoff 不是合法 JSON（{e}）", {}
        if not isinstance(d, dict):
            return False, "dev_handoff 必须是 JSON 对象", {}
        return True, "", d
    return False, f"dev_handoff 类型非法（{type(raw).__name__}，应为 JSON 对象/字符串）", {}


def validate_dev_handoff(raw_handoff: Any, node_id: str = "") -> tuple[bool, list[str]]:
    """SOP Gate 强校验。返回 (通过?, 缺失/格式问题清单)。
    调用方（executor）：任一问题 = 节点 FAIL，**不触发 git 提交**，发 sop_fail 事件。"""
    if raw_handoff is None:
        return False, ["dev_handoff 缺失（Dev 未交格式化交付说明）"]
    problems: list[str] = []
    ok, why, d = parse_handoff(raw_handoff)
    if not ok:
        return False, [why]
    # 必填字段逐个核（强契约：缺 = FAIL，非 warning）
    for f in REQUIRED_FIELDS:
        v = d.get(f)
        if f in ("commit_message", "pr_description"):
            if not (isinstance(v, str) and v.strip()):
                problems.append(f"dev_handoff.{f} 缺失或为空")
        elif f == "self_check":
            sc = v
            if not isinstance(sc, dict):
                problems.append("dev_handoff.self_check 必须是对象")
            else:
                for k in REQUIRED_SELF_CHECK_KEYS:
                    if k not in sc:
                        problems.append(f"dev_handoff.self_check.{k} 缺失（自查不完整）")
    # changed_files 可选，但给了必须 list（格式合法才算规范交接）
    if "changed_files" in d and not isinstance(d["changed_files"], list):
        problems.append("dev_handoff.changed_files 必须是 list")
    return (not problems), problems


def build_commit_message(handoff: dict[str, Any], node_id: str) -> str:
    """从合法 dev_handoff 生成规范化 commit message（node_id 前缀 + 说明），引擎代劳 git 用。"""
    base = str(handoff.get("commit_message") or "").strip()
    prefix = f"{node_id}:" if node_id else ""
    return f"{prefix} {base}".strip() or f"{node_id}: (空提交说明)"


class SopGateError(Exception):
    """SOP 强契约拦截（拍板 #4）：git 提交前 dev_handoff 不合法。
    引擎在 git_policy.dev_submit 里捕获 → 节点 FAIL + 不触发 git + 发 sop_fail 事件。"""


def gate_dev_submit(node_id: str, outputs: dict[str, str]) -> dict[str, Any]:
    """SOP Gate 拦截入口（dev_submit 前置闸门）：
    - outputs 无 dev_handoff 键 → 抛 SopGateError（Dev 未交格式化交付说明）
    - dev_handoff 格式/字段不合法 → 抛 SopGateError
    - 合法 → 返回解析后的 dict（build_commit_message 可复用）
    这是「build 成功 ≠ QA 过」红线在 Dev 交接层的落地：残缺/口头交接物理不进 git。"""
    if "dev_handoff" not in outputs:
        raise SopGateError(
            f"{node_id} 未按 SOP 交 dev_handoff（强契约，拍板 #4）：必须带 commit_message/"
            f"self_check/pr_description 的格式化交付说明，缺一即 FAIL，不触发 git 提交")
    ok, problems = validate_dev_handoff(outputs["dev_handoff"], node_id)
    if not ok:
        raise SopGateError(f"{node_id} dev_handoff 校验失败: " + "；".join(problems))
    _ok, _why, d = parse_handoff(outputs["dev_handoff"])
    return d
