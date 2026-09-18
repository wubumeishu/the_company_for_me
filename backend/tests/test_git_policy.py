"""Phase 3a 单测：Git 分支隔离 + PR 门禁（simulated backend）。

跑法：cd backend && python tests/test_git_policy.py
断言链：
  A) 分支隔离：dev_submit 只在自己分支 append，主干 head 不变
  B) PR 全绿：两 Dev 分支产物齐备 → merged + merge_sha（git 证据可溯源）
  C) 冲突红灯：同文件被两分支改 → pr_rejected（conflict 红旗）
  D) 门禁红灯：注入失败 checker → pr_rejected（脚本红灯，精确到名字）
  E) validator：pr_gate 无 git_policy 配置 → 422 拒（引擎权威层不可缺位）
  F) 全链路：workflows/mock-git-pr.json orchestrator 端到端成功 + 事件流含
     git_commit / pr_opened / pr_checks / pr_merged
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.engine.events import EventBus
from app.engine.git_policy import GitPolicy
from app.engine.orchestrator import Orchestrator
from app.persistence.progress import ProgressLog
from app.schema.validator import ContractError, LoadedWorkflow, load_workflow, _validate_v2

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, cond, detail))
    print(f"  {PASS if cond else FAIL}  {name}  {detail}")


def make_policy(gp: dict[str, Any] | None = None, agents: dict | None = None) -> GitPolicy:
    raw: dict[str, Any] = {
        "meta": {"name": "t", "version": "0"},
        "agents": agents or {"d1": {"id": "d1", "role": "Dev", "provider": "mock",
                                     "tools": [], "boundaries": {}}},
        "levels": [{"index": 0, "name": "L0", "nodes": [
            {"id": "n0", "agent": "d1", "task": "x"}]}],
        "qa": {"onFailure": ["console"]},
    }
    if gp is not None:
        raw["git_policy"] = gp
    wf = LoadedWorkflow(raw=raw, workflow_id="t", path=Path("."))
    return GitPolicy(wf, EventBus())


# ---------------------------------------------------------------- A) 分支隔离
async def test_branch_isolation():
    print("\n== A) 分支隔离（Dev 只提交到自己分支）==")
    gp = make_policy({"backend": "simulated", "baseBranch": "main"})
    node = {"id": "n1", "agent": "d1", "task": "实现模块A", "branch": "dev-a/n1"}
    base_head_before = gp.repo.heads.get("main")
    await gp.dev_submit(node, {"code": "print('A')"})
    check("Dev 提交落在 dev-a/n1 分支",
          gp.repo.heads.get("dev-a/n1") is not None and gp.repo.commits_on("dev-a/n1"))
    check("主干 main head 未被动过（引擎绝不直改）",
          gp.repo.heads.get("main") == base_head_before)


# ---------------------------------------------------------------- B) PR 全绿
async def test_pr_green():
    print("\n== B) PR 全绿（两 Dev 改不同文件 → merged + merge_sha）==")
    gp = make_policy({"backend": "simulated", "baseBranch": "main"})
    # 改不同文件（mod_a / mod_b）→ 无冲突
    await gp.dev_submit({"id": "n1", "task": "模块A", "branch": "dev-a"}, {"mod_a": "A文件"})
    await gp.dev_submit({"id": "n2", "task": "模块B", "branch": "dev-b"}, {"mod_b": "B文件"})
    pr, checks = await gp.open_pr({"id": "n3", "task": "合并A+B"}, ["n1", "n2"])
    check("PR state=merged", pr.state == "merged", f"state={pr.state} flags={pr.red_flags}")
    check("全门禁绿", all(c["passed"] for c in checks), str(checks))
    check("merge_sha 证据落主干（可溯源）",
          pr.merge_sha and gp.repo.commits_on("main")[-1].sha == pr.merge_sha)
    check("主干 squash 提交记录在案",
          gp.repo.commits_on("main") and "merge PR-" in gp.repo.commits_on("main")[-1].message)


# ---------------------------------------------------------------- C) 冲突红灯
async def test_pr_conflict():
    print("\n== C) 冲突红灯（同文件被两分支改 → rejected）==")
    gp = make_policy({"backend": "simulated", "baseBranch": "main"})
    # 两分支都改 shared.py（同键近似冲突）
    await gp.dev_submit({"id": "n1", "task": "改shared", "branch": "dev-a"}, {"shared": "vA"})
    await gp.dev_submit({"id": "n2", "task": "改shared", "branch": "dev-b"}, {"shared": "vB"})
    pr, checks = await gp.open_pr({"id": "n3", "task": "合并"}, ["n1", "n2"])
    c = next(x for x in checks if x["name"] == "conflict")
    check("conflict 门禁亮红灯", not c["passed"], c["detail"])
    check("PR state=rejected（拦截打回）", pr.state == "rejected")
    check("主干无 squash（拦截后引擎不改主干）",
          not gp.repo.commits_on("main"))


# ---------------------------------------------------------------- D) 门禁红灯
async def test_pr_gate_red():
    print("\n== D) 门禁红灯（注入失败 checker → 精确到名字）==")
    gp = make_policy({"backend": "simulated", "baseBranch": "main"})
    await gp.dev_submit({"id": "n1", "task": "x", "branch": "dev-a"}, {"code": "x"})

    def bad_checker(pr, ctx):
        return False, "eslint 3 处 style 违规"

    pr, checks = await gp.open_pr({"id": "n3", "task": "合并"}, ["n1"],
                                   checkers=[("lint", bad_checker)])
    # 注：checkers 覆盖默认内置；conflict 内置检查仍先跑
    c = next(x for x in checks if x["name"] == "lint")
    check("注入 lint checker 红灯", not c["passed"], c["detail"])
    check("PR rejected + 红旗含 lint", pr.state == "rejected" and any("lint" in f for f in pr.red_flags))


# ---------------------------------------------------------------- E) validator 422
def test_validator_pr_gate():
    print("\n== E) validator（pr_gate 无 git_policy → 拒）==")
    raw = {
        "meta": {"name": "t", "version": "0"},
        "agents": {"d1": {"id": "d1", "role": "Dev", "provider": "mock", "tools": [], "boundaries": {}}},
        "levels": [{"index": 0, "name": "L0", "nodes": [
            {"id": "n0", "agent": "d1", "kind": "pr_gate", "task": "门禁"}]}],
        "qa": {"onFailure": ["console"]},
    }
    try:
        _validate_v2(raw, raw["agents"])
        check("pr_gate 无 git_policy 被拒", False, "竟然放行了?!")
    except ContractError as e:
        check("pr_gate 无 git_policy 被拒", "git_policy" in str(e))

    # 配了 git_policy = 放行（向后兼容语义：有配置才校验）
    raw2 = dict(raw)
    raw2["git_policy"] = {"backend": "simulated"}
    try:
        _validate_v2(raw2, raw2["agents"])
        check("配 git_policy 后放行", True)
    except ContractError as e:
        check("配 git_policy 后放行", False, str(e))


# ---------------------------------------------------------------- F) 全链路
async def test_e2e_mock_git_pr():
    print("\n== F) 全链路（workflows/mock-git-pr.json 端到端）==")
    wf = load_workflow("mock-git-pr")
    bus = EventBus()
    evs: list[str] = []
    bus.subscribe(lambda e: evs.append(e.type))
    tmp = tempfile.mkdtemp(prefix="company-pr-")
    gp = GitPolicy(wf, bus)
    orch = Orchestrator(wf, bus, ProgressLog(tmp), checkpoint_root=tmp, git_policy=gp)
    res = await orch.run()

    check("run 成功（PR 全绿 → QA 拿到 merge_sha）", res["success"],
          f"failed_level={res['failed_level']}")
    n3 = res["results"].get("n3_qa") or {}
    check("QA 节点通过（merge_sha 数据流贯通）", n3.get("ok"), str(n3))
    need = {"git_commit", "pr_opened", "pr_checks", "pr_merged"}
    missing = need - set(evs)
    check("Git 实时流事件齐全（站会大盘数据源）", not missing,
          f"缺失={missing}" if missing else f"事件={sorted(set(evs))}")
    # 主干证据：main 有且仅有一条 squash 合并提交
    check("主干只有 squash 合并提交（引擎唯一合法写主干路径）",
          gp.repo.commits_on("main") and "merge PR-" in gp.repo.commits_on("main")[-1].message)


async def main() -> int:
    await test_branch_isolation()
    await test_pr_green()
    await test_pr_conflict()
    await test_pr_gate_red()
    test_validator_pr_gate()
    await test_e2e_mock_git_pr()
    failed = [r for r in results if not r[1]]
    print(f"\n{'='*40}\n总计 {len(results)} 项，通过 {len(results)-len(failed)}，失败 {len(failed)}")
    for name, _, detail in failed:
        print(f"  ✗ {name}: {detail}")
    print("全部 PASS ★ Phase 3a 分支隔离 + PR 门禁 验收通过" if not failed else "存在失败项")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
