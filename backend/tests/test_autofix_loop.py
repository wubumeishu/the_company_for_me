"""Phase 3b-④ 单测：红灯自动纠错回路（带错重做，maxAttempts=3）—— 灵魂机制。

跑法：cd backend && python tests/test_autofix_loop.py
真 Orchestrator 集成（mock Dev + 真 git + 真 pr_gate + 内容敏感门禁），复刻 main.py 接线：

  D-① 带错重做成功闭环：
       首跑：Dev(n1) 无 QA 反馈 → 产物无 FIXED 标记 → 内容敏感门禁 check_gate.py 红
             → orchestrator 回滚到 L0（Dev 行），把 pr_gate 红灯日志作为 qa_feedback 喂回
       重做：Dev(n1) 拿到反馈 → 产物补 FIXED 标记 → 门禁绿 → PR merge → run SUCCESS
       断言：run success + 恰好 1 次 rollback + main 物理推进 + FIXED 事件链路齐全

  D-② 恒红耗尽：门禁 check_always_red.py 永远红 → Dev 重做仍救不回来
       断言：重试 3 次（maxAttempts=3）彻底 FAILED + main 全程未动（红线）+ node_blocked 发出

checkScripts 用 tmp 种子仓内的真实脚本文件（跨平台无嵌套引号），venv --system-site-packages
隔离真跑（3b② 已验的 LOCAL-FIRST 套路）。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.engine.events import EventBus
from app.engine.subprocess_git_policy import SubprocessGitPolicy
from app.schema.validator import LoadedWorkflow

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, cond, detail))
    print(f"  {PASS if cond else FAIL}  {name}  {detail}")


def _seed(tmp: Path) -> None:
    """种子：两个门禁脚本（内容敏感 check_gate.py + 恒红 check_always_red.py）。"""
    (tmp / "README.md").write_text("# autofix loop seed\n", encoding="utf-8")
    # 内容敏感：读 Dev worktree 产物，含 FIXED 标记 → 绿；否则红（首跑/重做区分的关键）
    (tmp / "check_gate.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "p = Path('worktrees/n1/mod_a')\n"
        "if p.exists() and 'FIXED:' in p.read_text(encoding='utf-8'):\n"
        "    print('check_gate: 产物含 FIXED 修复标记，门禁绿')\n"
        "    sys.exit(0)\n"
        "print('check_gate: 产物 n1/mod_a 缺 FIXED 修复标记（未带 QA 反馈重做），门禁红')\n"
        "sys.exit(1)\n", encoding="utf-8")
    # 恒红：永远 exit 1（模拟 Dev 重做也救不回的缺陷）
    (tmp / "check_always_red.py").write_text(
        "import sys\nprint('check_always_red: 发现不可自愈缺陷，门禁红')\nsys.exit(1)\n",
        encoding="utf-8")


def _wf(check_script: str, max_attempts: int = 3) -> LoadedWorkflow:
    """单 Dev(n1) + pr_gate(n2_pr) + qa(n3_qa)，git_policy 走 subprocess + 内容敏感门禁。
    qa.rollback.targetLevel=0（回滚到 Dev 行）+ maxAttempts（拍板 #3）。"""
    raw = {
        "meta": {"name": "autofix", "version": "0"},
        "agents": {
            "dev": {"id": "dev", "role": "Dev", "provider": "mock",
                    "tools": ["write_file"], "outputs": [{"name": "mod_a", "kind": "text"}]},
            "reviewer": {"id": "reviewer", "role": "评审", "provider": "mock",
                        "tools": [], "outputs": [{"name": "review", "kind": "text"}]},
        },
        "git_policy": {"backend": "subprocess", "baseBranch": "main",
                       "checkScripts": [f"python {check_script}"], "checkTimeoutSec": 60},
        "levels": [
            {"index": 0, "name": "L0", "nodes": [
                {"id": "n1", "agent": "dev", "task": "模块A", "branch": "dev-a"}]},
            {"index": 1, "name": "L1", "nodes": [
                {"id": "n2_pr", "kind": "pr_gate", "agent": "reviewer", "task": "门禁",
                 "inputs": ["n1.mod_a"]}]},
            {"index": 2, "name": "L2", "nodes": [
                {"id": "n3_qa", "kind": "qa", "agent": "reviewer", "task": "验收",
                 "inputs": ["n2_pr.merge_sha"]}]},
        ],
        "qa": {"onFailure": ["console"],
                "rollback": {"targetLevel": 0},
                "retry": {"maxAttempts": max_attempts}},
    }
    return LoadedWorkflow(raw=raw, workflow_id="autofix", path=Path("."))


async def _run(tmp: Path, check_script: str, max_attempts: int = 3):
    """复刻 main.py 接线：subprocess → SubprocessGitPolicy → 真实 Orchestrator。"""
    from app.engine.orchestrator import Orchestrator
    from app.persistence.progress import ProgressLog
    wf = _wf(check_script, max_attempts)
    bus = EventBus()
    evs: list[dict] = []
    bus.subscribe(lambda e: evs.append({"t": e.type, **e.data}))
    gp = SubprocessGitPolicy(wf, bus, project_root=tmp)
    await gp.repo.ensure_repo()
    head0 = await gp.repo.main_head()
    orch = Orchestrator(wf, bus, ProgressLog(str(tmp)), checkpoint_root=str(tmp),
                        git_policy=gp)
    res = await orch.run()
    head1 = await gp.repo.main_head()
    return res, evs, gp, head0, head1


async def test_autofix_success_loop():
    print("\n== D-① 带错重做成功闭环（首跑红 → 回滚注入 QA 反馈 → 重做绿 → merge）==")
    tmp = Path(tempfile.mkdtemp(prefix="company-fix-"))
    _seed(tmp)
    res, evs, gp, head0, head1 = await _run(tmp, "check_gate.py", max_attempts=3)

    rollbacks = [e for e in evs if e["t"] == "rollback"]
    check("run 最终 SUCCESS（带错重做救回）", res["success"], f"failed_level={res['failed_level']}")
    check("恰好 1 次 rollback（首跑红→回滚→重做绿，不浪费）", len(rollbacks) == 1,
          f"rollback 次数={len(rollbacks)}")
    check("回滚目标是 L0（Dev 行，带错重做发生地）",
          all(e["to_level"] == 0 for e in rollbacks),
          str([e["to_level"] for e in rollbacks]))
    check("★ 全绿后 main 物理推进", head1 != head0, f"{head0[:10]}→{head1[:10]}")
    # 带错重做链路：qa_feedback 注入事件 + 重跑后 FIXED 产物
    merged = [e for e in evs if e["t"] == "pr_merged"]
    check("PR 最终 merged（重做后全绿收编）", len(merged) >= 1, f"merged={len(merged)}")
    check("QA 节点拿到 merge_sha（数据流贯通）",
          (res["results"].get("n3_qa") or {}).get("ok"), str(res["results"].get("n3_qa")))
    # ★ 带错重做的真正证据：merge 进 main 后，Dev 重做产出的文件里带着 FIXED 标记
    #   （首跑无反馈无标记 → 门禁红；回滚注入 qa_feedback → 重做带标记 → 门禁绿并合入主干）
    main_mod_a = (gp.repo.root / "mod_a")
    content = main_mod_a.read_text(encoding="utf-8") if main_mod_a.exists() else ""
    check("main 上的产物含 FIXED 标记（qa_feedback 生效并合入主干）",
          "FIXED:" in content, f"mod_a={'(缺)' if not main_mod_a.exists() else content[:50]!r}")


async def test_autofix_exhausted():
    print("\n== D-② 恒红耗尽（maxAttempts=3 重试 3 次仍红 → 彻底 FAILED + main 未动）==")
    tmp = Path(tempfile.mkdtemp(prefix="company-fixred-"))
    _seed(tmp)
    res, evs, gp, head0, head1 = await _run(tmp, "check_always_red.py", max_attempts=3)

    rollbacks = [e for e in evs if e["t"] == "rollback"]
    rejected = [e for e in evs if e["t"] == "pr_rejected"]
    blocked = [e for e in evs if e["t"] == "node_blocked"]
    check("run 彻底 FAILED（3 次纠错救不回）", not res["success"], f"failed_level={res['failed_level']}")
    check("★ 恰好重试 3 次（maxAttempts=3 拍板）才放弃", len(rollbacks) == 3,
          f"rollback 次数={len(rollbacks)}")
    check("PR 红灯 4 次（首跑 + 3 次重做都红）", len(rejected) == 4, f"rejected={len(rejected)}")
    check("★ 红线：恒红全程 main 未动", head1 == head0, f"{head0[:10]}={head1[:10]}")
    check("§1.4 node_blocked 发出（pr_gate + 下游 qa 锁链）", len(blocked) >= 1,
          f"blocked={len(blocked)}")


async def main() -> int:
    await test_autofix_success_loop()
    await test_autofix_exhausted()
    failed = [r for r in results if not r[1]]
    print(f"\n{'='*44}\n总计 {len(results)} 项，通过 {len(results)-len(failed)}，失败 {len(failed)}")
    for name, _, detail in failed:
        print(f"  ✗ {name}: {detail}")
    print("全部 PASS ★ Phase 3b-④ 带错重做闭环（提交→拦截→带错重做→再提交全绿，maxAttempts=3）验收通过"
          if not failed else "存在失败项")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
