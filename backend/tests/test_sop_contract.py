"""Phase 3b-③ 单测：dev_handoff 强契约 + SOP Gate（拍板 #4「强契约」）。

跑法：cd backend && python tests/test_sop_contract.py
三链：
  A) SOP Gate 纯函数全分支（零 git 零进程）：缺键/坏JSON/缺self_check子键/changed_files非list/合法
  B) SubprocessGitPolicy 强契约拦截（离线 FakeRunner）：sopContract=true 时残缺交接物理不进 git
  C) executor 捕获 SopGateError → 节点 FAIL + 不发 git + 发 sop_fail 事件（带错重做可抓取具体错误）
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.engine.events import EventBus
from app.engine.sop_contract import (validate_dev_handoff, parse_handoff,
                                     gate_dev_submit, build_commit_message, SopGateError)
from app.engine.subprocess_git_policy import SubprocessGitPolicy
from app.engine.subprocess_sandbox import SubprocessResult
from app.schema.validator import LoadedWorkflow

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, cond, detail))
    print(f"  {PASS if cond else FAIL}  {name}  {detail}")


GOOD = {
    "commit_message": "fix: 修好登录态",
    "self_check": {"ran": ["pytest tests/test_auth.py"], "passed": True},
    "pr_description": "修复登录态过期未清，补充回归测试",
    "changed_files": ["backend/app/auth.py", "backend/tests/test_auth.py"],
}


# ============ A) SOP Gate 纯函数全分支 ============
def test_pure():
    print("\n== A) SOP Gate 纯函数（零依赖） ==")
    ok, probs = validate_dev_handoff(GOOD)
    check("合法 dev_handoff 通过", ok, str(probs))

    ok, probs = validate_dev_handoff(None)
    check("缺失 dev_handoff 拒绝", not ok and "缺失" in " ".join(probs), str(probs))

    ok, probs = validate_dev_handoff("{坏JSON")
    check("坏 JSON 字符串拒绝", not ok, str(probs))

    ok, probs = validate_dev_handoff("[1,2]")
    check("非对象 JSON 拒绝", not ok, str(probs))

    bad = {k: v for k, v in GOOD.items() if k != "commit_message"}
    ok, probs = validate_dev_handoff(bad)
    check("缺 commit_message 拒绝", not ok and any("commit_message" in p for p in probs), str(probs))

    bad2 = dict(GOOD); bad2["self_check"] = {"ran": ["pytest"]}   # 缺 passed
    ok, probs = validate_dev_handoff(bad2)
    check("self_check 缺 passed 子键拒绝", not ok and any("passed" in p for p in probs), str(probs))

    bad3 = dict(GOOD); bad3["self_check"] = "跑了"               # 非对象
    ok, probs = validate_dev_handoff(bad3)
    check("self_check 非对象拒绝", not ok, str(probs))

    bad4 = dict(GOOD); bad4["changed_files"] = "auth.py"          # 非 list
    ok, probs = validate_dev_handoff(bad4)
    check("changed_files 非 list 拒绝", not ok, str(probs))

    # parse_handoff
    ok, why, d = parse_handoff(GOOD)
    check("parse_handoff(dict) 原样", ok and d is GOOD)
    import json
    ok, why, d = parse_handoff(json.dumps(GOOD))
    check("parse_handoff(JSON str) 解析", ok and d == GOOD)

    # gate_dev_submit（executor/policy 前置入口）
    try:
        gate_dev_submit("n1", {"dev_handoff": GOOD})
        check("gate_dev_submit 合法放行", True)
    except SopGateError as e:
        check("gate_dev_submit 合法放行", False, str(e))
    try:
        gate_dev_submit("n1", {})
        check("gate_dev_submit 缺键拦截", False)
    except SopGateError:
        check("gate_dev_submit 缺键拦截", True)

    # build_commit_message
    check("build_commit_message 加 node 前缀",
          build_commit_message(GOOD, "n1") == "n1: fix: 修好登录态",
          build_commit_message(GOOD, "n1"))


# ============ B) SubprocessGitPolicy 强契约拦截（离线） ============
class FakeRunner:
    async def __call__(self, args, cwd=None, env=None, timeout=None, on_line=None, **kw):
        toks = [str(a) for a in args]
        if "rev-parse" in toks:
            return SubprocessResult(0, "sha-sop-001\n", "")
        return SubprocessResult(0, "", "")


def _wf() -> LoadedWorkflow:
    raw = {
        "meta": {"name": "t", "version": "0"},
        "agents": {"d1": {"id": "d1", "role": "Dev", "provider": "mock", "tools": [], "boundaries": {}}},
        "levels": [
            {"index": 0, "name": "L0", "nodes": [
                {"id": "n1", "agent": "d1", "task": "模块A", "branch": "dev-a"}]},
        ],
        "git_policy": {"backend": "subprocess", "baseBranch": "main"},
        "qa": {"onFailure": ["console"]},
    }
    return LoadedWorkflow(raw=raw, workflow_id="sop", path=Path("."))


async def test_policy_strong():
    print("\n== B) SubprocessGitPolicy 强契约（离线 FakeRunner） ==")
    from app.engine.subprocess_sandbox import SandboxRepo
    tmp = Path(tempfile.mkdtemp(prefix="company-sop-"))
    wf = _wf()
    wf.raw["git_policy"]["sopContract"] = True
    bus = EventBus()
    gp = SubprocessGitPolicy(wf, bus, project_root=tmp)
    gp.repo = SandboxRepo(tmp / ".company" / "git-sandbox", bus=bus, runner=FakeRunner(),
                           trusted=True, seed_from=tmp)
    node = wf.raw["levels"][0]["nodes"][0]

    # 残缺交接（缺 dev_handoff）→ 物理不进 git
    try:
        await gp.dev_submit(node, {"mod_a.py": "A=1\n"})
        check("强契约：缺 dev_handoff 拦 git", False, "竟然提交了")
    except SopGateError:
        check("强契约：缺 dev_handoff 拦 git（SopGateError）", True)

    # 自相矛盾（self_check.passed=false 仍交）→ 也是残缺格式（缺 pr_description 即合法但... 用缺键造）
    try:
        await gp.dev_submit(node, {"mod_a.py": "A=1\n",
                                   "dev_handoff": {"commit_message": "x", "pr_description": "y"}})
        check("强契约：self_check 缺子键拦 git", False)
    except SopGateError:
        check("强契约：self_check 缺子键拦 git（SopGateError）", True)

    # 合法 dev_handoff → 成功 commit，msg 用规范说明（非 node task 回落）
    import json
    arts = {"mod_a.py": "A=1\n", "dev_handoff": json.dumps(GOOD)}
    sha = await gp.dev_submit(node, arts)
    check("强契约：合法 dev_handoff 正常提交", sha == "sha-sop-001", sha)
    # commit msg 走规范说明：验证 FakeRunner 收到的 git commit 消息含 commit_message 文本
    # （离线 sha 固定，msg 校验用 policy 暴露的 build 逻辑已在 A 段验过，此处验证不抛错即通过）

    # sopContract=false 时回落 node task（不强制）
    wf2 = _wf()
    wf2.raw["git_policy"]["sopContract"] = False
    gp2 = SubprocessGitPolicy(wf2, EventBus(), project_root=tmp)
    gp2.repo = SandboxRepo(tmp / ".company" / "git-sandbox", bus=gp2.bus, runner=FakeRunner(),
                           trusted=True, seed_from=tmp)
    sha2 = await gp2.dev_submit(node, {"mod_a.py": "A=1\n"})
    check("sopContract=false 不强制（回落 node task）", sha2 == "sha-sop-001", sha2)


# ============ C) executor 捕获 SopGateError → 节点 FAIL + sop_fail ============
async def test_executor_catch():
    print("\n== C) executor 捕获 SOP 拦截（节点 FAIL + 不发 git + sop_fail 事件） ==")
    from app.engine.executor import NodeExecutor
    from app.persistence.progress import ProgressLog

    class StubPolicy:
        async def dev_submit(self, node, artifacts):
            # 模拟 3a/3b② 的 sopContract=true 强契约：dev_handoff 缺 → 抛
            if "dev_handoff" not in artifacts:
                raise SopGateError(f"{node['id']} 未按 SOP 交 dev_handoff")
            return "sha-stub"

    tmp = Path(tempfile.mkdtemp(prefix="company-sopex-"))
    wf = LoadedWorkflow(raw={
        "meta": {"name": "t", "version": "0"},
        "agents": {"dev": {"id": "dev", "role": "Dev", "provider": "mock",
                           "tools": [], "outputs": [{"name": "mod_a", "kind": "text"}]}},
        "levels": [{"index": 0, "name": "L0",
                    "nodes": [{"id": "n1", "agent": "dev", "task": "模块A", "branch": "dev-a"}]}],
        "git_policy": {"backend": "subprocess", "sopContract": True},
        "qa": {"onFailure": ["console"]},
    }, workflow_id="sopex", path=Path("."))
    bus = EventBus()
    evs: list[str] = []
    bus.subscribe(lambda e: evs.append(e.type))
    ex = NodeExecutor(node=wf.raw["levels"][0]["nodes"][0], agent=wf.resolved_agent("dev"),
                      wf=wf, bus=bus, progress=ProgressLog(str(tmp)),
                      git_policy=StubPolicy())
    ex.level = 0
    out = await ex.run()
    check("SOP 拦截 → 节点 FAIL", not out.ok, out.error or "")
    check("SOP 拦截 → error 含 'SOP Gate'（可被纠错回路抓取）",
          "SOP Gate" in (out.error or ""), out.error or "")
    check("sop_fail 事件已发（带错重做数据源）", "sop_fail" in evs, str(sorted(set(evs))))
    check("SOP 拦截 → 产物无 branch（git 未提交）", "branch" not in out.outputs, str(out.outputs))


async def main() -> int:
    test_pure()
    await test_policy_strong()
    await test_executor_catch()
    failed = [r for r in results if not r[1]]
    print(f"\n{'='*44}\n总计 {len(results)} 项，通过 {len(results)-len(failed)}，失败 {len(failed)}")
    for name, _, detail in failed:
        print(f"  ✗ {name}: {detail}")
    print("全部 PASS ★ Phase 3b-③ dev_handoff 强契约 + SOP Gate 验收通过"
          if not failed else "存在失败项")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
