"""Phase 3b-④ 单测：中介授权层 Authorize Gate（Agency-Agents Middleware Auth Layer）。

跑法：cd backend && python tests/test_authorize_gate.py
三链：
  A) authorize 纯判定全分支（副作用分级 + 白名单 + grant + approval 断点，零进程）
  B) 默认副作用表对齐平台工具词汇表（patch/compiler=local_write 不误伤，browser=external）
  C) executor 前置鉴权：external_side 工具无 grant → AuthGateError → 节点 FAIL + auth_block 事件
     + 有 grant 的 external 放行 + 高风险无 approval 令牌仍拒
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.engine.authorize_gate import (authorize, side_effect_of, Verdict,
                                       DEFAULT_SIDE_EFFECTS, HIGH_RISK_TOOLS, AuthGateError)
from app.engine.events import EventBus
from app.schema.validator import LoadedWorkflow

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, cond, detail))
    print(f"  {PASS if cond else FAIL}  {name}  {detail}")


def _agent(tools=None, allowed=None, grants=None, side_effects=None):
    b = {}
    if allowed is not None:
        b["allowedTools"] = allowed
    if grants is not None:
        b["externalGrants"] = grants
    if side_effects is not None:
        b["sideEffects"] = side_effects
    return {"tools": tools or [], "boundaries": b}


# ============ A) authorize 纯判定全分支 ============
def test_pure():
    print("\n== A) authorize 判定（副作用分级 AND 逻辑） ==")
    # none（纯读）：白名单内直接放行
    v = authorize("read_file", _agent(tools=["read_file"]))
    check("none 纯读放行", v.allowed and v.side_effect == "none", v.reason)

    # 白名单外：直接拒（最保守档）
    v = authorize("read_file", _agent(tools=["write_file"]))
    check("白名单外拒绝", not v.allowed and "白名单" in v.reason, v.reason)

    # local_write（影子仓内写）：白名单内放行（引擎单写入口代劳）
    v = authorize("write_file", _agent(tools=["write_file"]))
    check("local_write 白名单内放行", v.allowed and v.side_effect == "local_write", v.reason)

    # external_side 无 grant：拒（最安全档）
    v = authorize("send_email", _agent(tools=["send_email"]))
    check("external 无 grant 拒绝", not v.allowed and "grant" in v.reason, v.reason)

    # external_side 有 grant + 非高风险：放行
    v = authorize("third_party_api", _agent(tools=["third_party_api"], grants=["third_party_api"]))
    check("external 有 grant 放行", v.allowed, v.reason)

    # 高风险 external + grant 但无 approval 令牌：仍拒（不静默放行）
    v = authorize("push_remote", _agent(tools=["push_remote"], grants=["push_remote"]))
    check("高风险无 approval 令牌拒绝", not v.allowed and "审批断点" in v.reason, v.reason)

    # 高风险 external + grant + approval 令牌：放行（预留断点）
    v = authorize("push_remote", _agent(tools=["push_remote"], grants=["push_remote"]),
                  approval_token="APPR-77")
    check("高风险有 approval 令牌放行", v.allowed and v.approval == "APPR-77", v.reason)

    # allowedTools 显式名单优先于 tools
    v = authorize("git", _agent(tools=["git"], allowed=["git", "write_file"]))
    check("allowedTools 显式名单放行 git", v.allowed, v.reason)

    # unknown 工具（不在默认表）= 保守 external_side
    v = authorize("magic_wand", _agent(tools=["magic_wand"]))
    check("unknown 工具按 external_side 保守档", v.side_effect == "external_side", v.side_effect)
    check("unknown 工具无 grant 拒", not v.allowed, v.reason)


# ============ B) 默认副作用表对齐平台工具词汇表 ============
def test_vocab_alignment():
    print("\n== B) 默认副作用表对齐 TOOL_VOCAB（不误伤本地工具） ==")
    # 平台词汇表（org.py TOOL_VOCAB）里的工具，除 browser 外都应是本地/读（不 external）
    for t in ("read_file", "web_search", "web_extract",
              "write_file", "patch", "terminal", "git", "compiler"):
        se = side_effect_of(t, _agent(tools=[t]))
        check(f"{t} 非 external（= {se}）", se in ("none", "local_write"), se)
    # browser = 真实出外网 → external_side
    check("browser = external_side（出外网）", side_effect_of("browser", _agent(tools=["browser"])) == "external_side")
    # 未知工具保守
    check("未声明工具默认 external_side（保守）", side_effect_of("totally_new_tool", _agent(tools=["totally_new_tool"])) == "external_side")
    # agent 级 sideEffects 覆盖默认
    override = _agent(tools=["deploy"], side_effects={"deploy": "none"})
    check("agent.sideEffects 覆盖默认（deploy→none 放行）",
          authorize("deploy", override).allowed)


# ============ C) executor 前置鉴权（AuthGateError → 节点 FAIL + auth_block） ============
async def test_executor_gate():
    print("\n== C) executor 前置鉴权（external 无 grant 拦截 + 事件可回放） ==")
    from app.engine.executor import NodeExecutor
    from app.persistence.progress import ProgressLog

    tmp = Path(tempfile.mkdtemp(prefix="company-auth-"))

    def _wf(external_grant=False):
        b = {"externalGrants": ["send_email"]} if external_grant else {}
        raw = {
            "meta": {"name": "t", "version": "0"},
            "agents": {"ops": {"id": "ops", "role": "Ops", "provider": "mock",
                               "tools": ["send_email"], "boundaries": b,
                               "outputs": [{"name": "report", "kind": "text"}]}},
            "levels": [{"index": 0, "name": "L0",
                        "nodes": [{"id": "n1", "agent": "ops", "task": "发邮件",
                                    "toolOverrides": ["send_email"]}]}],
            "qa": {"onFailure": ["console"]},
        }
        return LoadedWorkflow(raw=raw, workflow_id="auth", path=Path("."))

    # 无 grant → 拦截
    wf = _wf(False)
    bus = EventBus()
    evs: list[str] = []
    bus.subscribe(lambda e: evs.append(e.type))
    ex = NodeExecutor(node=wf.raw["levels"][0]["nodes"][0], agent=wf.resolved_agent("ops"),
                      wf=wf, bus=bus, progress=ProgressLog(str(tmp)))
    ex.level = 0
    out = await ex.run()
    check("external 无 grant → 节点 FAIL", not out.ok, out.error or "")
    check("error 含 '授权闸门'（Agent 自己意识到没权限）", "授权闸门" in (out.error or ""), out.error or "")
    check("auth_block 事件已发", "auth_block" in evs, str(sorted(set(evs))))
    check("tool_authorize 事件可回放", "tool_authorize" in evs)

    # 有 grant → 放行（send_email 非 HIGH_RISK? 它其实是高风险，需 approval → 仍拒；这里测一般 external）
    # 改用 third_party_api（非高风险）做"有 grant 放行"的干净档
    wf2 = LoadedWorkflow(raw={
        "meta": {"name": "t", "version": "0"},
        "agents": {"api": {"id": "api", "role": "API", "provider": "mock",
                            "tools": ["third_party_api"],
                            "boundaries": {"externalGrants": ["third_party_api"]},
                            "outputs": [{"name": "report", "kind": "text"}]}},
        "levels": [{"index": 0, "name": "L0",
                    "nodes": [{"id": "n1", "agent": "api", "task": "调API",
                                "toolOverrides": ["third_party_api"]}]}],
        "qa": {"onFailure": ["console"]},
    }, workflow_id="auth-ok", path=Path("."))
    bus2 = EventBus()
    ex2 = NodeExecutor(node=wf2.raw["levels"][0]["nodes"][0], agent=wf2.resolved_agent("api"),
                       wf=wf2, bus=bus2, progress=ProgressLog(str(tmp)))
    ex2.level = 0
    out2 = await ex2.run()
    check("external 有 grant（非高风险）→ 放行节点通过", out2.ok, out2.error or "")

    # 高风险 send_email 即便有 grant 仍需 approval → 无令牌仍拒
    wf3 = LoadedWorkflow(raw={
        "meta": {"name": "t", "version": "0"},
        "agents": {"mail": {"id": "mail", "role": "Mail", "provider": "mock",
                             "tools": ["send_email"],
                             "boundaries": {"externalGrants": ["send_email"]},
                             "outputs": [{"name": "report", "kind": "text"}]}},
        "levels": [{"index": 0, "name": "L0",
                    "nodes": [{"id": "n1", "agent": "mail", "task": "发高风险邮件",
                                "toolOverrides": ["send_email"]}]}],
        "qa": {"onFailure": ["console"]},
    }, workflow_id="auth-hi", path=Path("."))
    bus3 = EventBus()
    ex3 = NodeExecutor(node=wf3.raw["levels"][0]["nodes"][0], agent=wf3.resolved_agent("mail"),
                       wf=wf3, bus=bus3, progress=ProgressLog(str(tmp)))
    ex3.level = 0
    out3 = await ex3.run()
    check("高风险 external 有 grant 但无 approval 令牌 → 仍拒（不静默放行）",
          not out3.ok and "审批断点" in (out3.error or ""), out3.error or "")


async def main() -> int:
    test_pure()
    test_vocab_alignment()
    await test_executor_gate()
    failed = [r for r in results if not r[1]]
    print(f"\n{'='*44}\n总计 {len(results)} 项，通过 {len(results)-len(failed)}，失败 {len(failed)}")
    for name, _, detail in failed:
        print(f"  ✗ {name}: {detail}")
    print("全部 PASS ★ Phase 3b-④ 中介授权层（副作用分级 + 闸门 + approval 断点）验收通过"
          if not failed else "存在失败项")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
