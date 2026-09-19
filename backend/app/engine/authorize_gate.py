"""
authorize_gate.py —— Phase 3b-④：工具白名单之上的中介授权层（Agency-Agents Middleware Auth Layer）。

架构书 §1.5（主人拍板）：AI 员工不能只在沙盒里自娱自乐，想碰真实外部服务
（发邮件 / 推远程仓 / 调第三方 API）必须先过一层**标准化中介授权**。本模块就是
那把闸门——在执行器调用工具前拦住它，按副作用分级 + 白名单 + grant 三条 AND 放行，
全程可回放（tool_authorize 事件），高风险 external 默认拒绝（最安全档），
并预留 approval 断点（human-in-the-loop，未来接 WS 弹窗，接口已就位）。

副作用分级（side_effect 标签，配在 agent.boundaries.sideEffects 或工具默认表）：
  none          纯读 / 纯本地脚本，零真实世界影响（沙盒自娱）
  local_write   写本地文件 / worktree（影子仓内，引擎单写入口管）
  external_side 带真实外部副作用（发邮件 / 推远程 / 调 3rd API）← 必须过层③

判定（AND 全过才放行）：
  1) 工具在 agent 白名单（boundaries.allowedTools / tools）
  2) side_effect != external_side，或 external 且 agent 被显式授予该外部 grant
  3) 高风险 external（命中 HIGH_RISK_TOOLS）→ 需 approval 断点；
     本阶段默认「无 approval 令牌 = 拒绝」（不静默放行），令牌位预留
返回 Verdict(allowed, reason, side_effect, approval) —— 放行/拒绝都发 tool_authorize 事件。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

# 默认工具副作用标签（对齐 org.py 平台工具词汇表 TOOL_VOCAB + 引擎工具，
# agent.boundaries.sideEffects 可覆盖单个工具）。未知工具名 = 按最保守档 external_side。
DEFAULT_SIDE_EFFECTS: dict[str, str] = {
    # 纯读 / 检索：零真实世界影响（none）
    "read_file": "none",
    "list_files": "none",
    "search": "none",
    "think": "none",
    "web_search": "none",
    "web_extract": "none",
    # 本地写 / 编译 / 版本控制：影子仓内，引擎单写入口代劳（local_write）
    "write_file": "local_write",
    "edit": "local_write",
    "patch": "local_write",
    "terminal": "local_write",
    "git": "local_write",
    "compiler": "local_write",
    # 真实外部副作用：碰真实世界（出外网 / 邮件 / 推远程 / 部署）← 必须过授权层
    "browser": "external_side",
    "send_email": "external_side",
    "push_remote": "external_side",
    "third_party_api": "external_side",
    "deploy": "external_side",
}

# 高风险 external（即使有 grant，也必须 approval 断点放行）
HIGH_RISK_TOOLS = {"push_remote", "deploy", "send_email"}


@dataclass
class Verdict:
    allowed: bool
    reason: str
    side_effect: str
    approval: Optional[str] = None      # 命中高风险且需人工审批时的断点令牌（预留）

    def is_blocked_by_auth(self) -> bool:
        return not self.allowed


class AuthGateError(Exception):
    """中介授权层拦截：external_side 工具未获 grant / approval，或白名单外。
    执行器在调用工具前过闸门，命中即抛——让 Agent 自己意识到"没有权限"（不静默放行）。"""


def side_effect_of(tool: str, agent: dict[str, Any]) -> str:
    """工具副作用标签：agent.boundaries.sideEffects 覆盖 > 默认表 > unknown=external（保守）。"""
    override = (agent.get("boundaries") or {}).get("sideEffects") or {}
    if tool in override:
        return str(override[tool])
    if tool in DEFAULT_SIDE_EFFECTS:
        return DEFAULT_SIDE_EFFECTS[tool]
    return "external_side"   # 未声明副作用的外部工具，按最保守档处理


def _in_whitelist(tool: str, agent: dict[str, Any]) -> bool:
    """层① 白名单：boundaries.allowedTools（显式名单）或 agent.tools（能力声明）。"""
    b = agent.get("boundaries") or {}
    if b.get("allowedTools") is not None:
        return tool in b["allowedTools"]
    return tool in (agent.get("tools") or [])


def _has_external_grant(tool: str, agent: dict[str, Any]) -> bool:
    """external_side 工具需 agent.boundaries.externalGrants 里显式列出该工具。"""
    b = agent.get("boundaries") or {}
    return tool in (b.get("externalGrants") or [])


def authorize(tool: str, agent: dict[str, Any],
              approval_token: Optional[str] = None) -> Verdict:
    """中介授权闸门：副作用分级 + 白名单 + grant + approval 断点，AND 全过才放行。

    approval_token：human-in-the-loop 已签发的审批断点令牌（本阶段外部传入，默认 None
    = 无审批 → 高风险 external 一律拒绝；未来由 WS 审批弹窗签发后经引擎注入）。"""
    side = side_effect_of(tool, agent)

    # 层① 白名单（防绕过）
    if not _in_whitelist(tool, agent):
        return Verdict(False, f"工具 {tool} 不在白名单", side)

    if side == "none":
        return Verdict(True, "纯读/本地脚本（无副作用），放行", side)

    if side == "local_write":
        return Verdict(True, "本地写（影子仓内），引擎单写入口代劳，放行", side)

    # side == external_side：层③ 强制中介授权
    if not _has_external_grant(tool, agent):
        return Verdict(False, f"external 副作用 {tool} 未被授予 grant（最安全档拒绝）", side)
    # 命中高风险 → 必须 approval 断点（令牌）；无令牌 = 拒绝（本阶段不静默放行）
    if tool in HIGH_RISK_TOOLS:
        if approval_token:
            return Verdict(True, f"高风险 {tool} 已签发审批断点，放行", side,
                           approval=approval_token)
        return Verdict(False, f"高风险 external {tool} 需人类审批断点（approval 未签发）", side,
                       approval=None)
    # 一般 external + 有 grant → 放行（可回放）
    return Verdict(True, f"external {tool} 已授予 grant，放行", side)
