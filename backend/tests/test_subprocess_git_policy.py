"""Phase 3b-② 单测：真 git 分支路由 + PR 门禁 + venv 真跑 + §1.4 BLOCKED 传播。

跑法：cd backend && python tests/test_subprocess_git_policy.py
双链（同 3b① 思路）：
  A) 离线 FakeRunner：dev_submit/open_pr 的分支路由 + 三段式 + node_blocked 全程零真进程
  B) 真 git + 真 venv（tmp 种子仓，绝不碰主仓 .git）：
     ① 全绿（两 Dev 改不同文件 + 绿脚本）→ main ff 推进
     ② 冲突红（同文件 add/add）→ rejected + main 未动 + node_blocked(pr_gate+下游)
     ③ 脚本红（venv 真跑红脚本）→ rejected + main 未动 + node_blocked
     ★ 红线：每条红灯路径 main head 全程未动（物理保证实测）

checkScripts 用种子仓内的真实脚本文件（跨平台无嵌套引号，符合真实门禁形态），
venv 以 --system-site-packages 隔离执行环境（LOCAL-FIRST 复用系统解释器，零网络）。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.engine.events import EventBus
from app.engine.subprocess_git_policy import SubprocessGitPolicy, downstream_of
from app.engine.subprocess_sandbox import SubprocessResult
from app.schema.validator import LoadedWorkflow

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, cond, detail))
    print(f"  {PASS if cond else FAIL}  {name}  {detail}")


def make_wf() -> LoadedWorkflow:
    raw = {
        "meta": {"name": "t", "version": "0"},
        "agents": {"d1": {"id": "d1", "role": "Dev", "provider": "mock",
                          "tools": [], "boundaries": {}}},
        "levels": [
            {"index": 0, "name": "L0", "nodes": [
                {"id": "n1", "agent": "d1", "task": "模块A", "branch": "dev-a"},
                {"id": "n2", "agent": "d1", "task": "模块B", "branch": "dev-b"}]},
            {"index": 1, "name": "L1", "nodes": [
                {"id": "n2_pr", "kind": "pr_gate", "agent": "d1", "task": "门禁",
                 "inputs": ["n1.mod_a", "n2.mod_b"]}]},
            {"index": 2, "name": "L2", "nodes": [
                {"id": "n3_qa", "kind": "qa", "agent": "d1", "task": "验收",
                 "inputs": ["n2_pr.merge_sha"]}]},
        ],
        "qa": {"onFailure": ["console"]},
    }
    return LoadedWorkflow(raw=raw, workflow_id="t", path=Path("."))


# ============ A) 离线 FakeRunner（零真进程） ============
class FakeRunner:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.script_rc = 0

    async def __call__(self, args, cwd=None, env=None, timeout=None, on_line=None, **kw):
        toks = [str(a) for a in args]
        self.calls.append(toks)
        # shell 脚本（venv 档 checkScripts）→ 按 script_rc 桩
        if toks and toks[0] in ("cmd", "/bin/sh"):
            return SubprocessResult(self.script_rc, "out", "err")
        # git 语义桩（识别 rev-parse 即便带 -C 前缀）
        if len(toks) > 1 and toks[0] == "git":
            if "rev-parse" in toks:
                return SubprocessResult(0, "sha-x-001\n", "")
            sub = toks[1]
            if sub == "diff" and toks[2:4] == ["--cached", "--quiet"]:
                return SubprocessResult(1, "", "")   # 有 staged
            if sub == "branch" and toks[2] == "-D":
                return SubprocessResult(0, "", "")
            if sub == "merge" and (toks[2] in ("--squash", "--ff-only")):
                return SubprocessResult(0, "", "")
            if sub == "merge-tree":
                return SubprocessResult(0, "", "")
        return SubprocessResult(0, "", "")


async def test_offline():
    print("\n== A) 离线 FakeRunner（零真进程） ==")
    from app.engine.subprocess_sandbox import SandboxRepo
    fake = FakeRunner()
    tmp = Path(tempfile.mkdtemp(prefix="company-p2-"))
    wf = make_wf()
    wf.raw["git_policy"] = {"backend": "subprocess", "baseBranch": "main"}
    gp = SubprocessGitPolicy(wf, EventBus(), project_root=tmp)
    gp.repo = SandboxRepo(tmp / ".company" / "git-sandbox", bus=gp.bus,
                           runner=fake, trusted=True, seed_from=tmp)

    # dev_submit 分支路由（dev-a 自定义 branch + 引擎代劳 commit）
    node_n1 = wf.raw["levels"][0]["nodes"][0]
    sha = await gp.dev_submit(node_n1, {"mod_a": "print('A')"})
    check("dev_submit → 引擎代劳 commit（离线 sha）", sha == "sha-x-001", sha)
    check("dev_submit 用自定义 branch dev-a",
          any("dev-a" in " ".join(c) for c in fake.calls))

    # downstream BFS 纯函数（§1.4）
    ds = downstream_of(wf, ["n2_pr"])
    ids = [nid for _, nid in ds]
    check("BLOCKED 下游 BFS：n2_pr + n3_qa（pr_gate 红灯连锁）",
          "n2_pr" in ids and "n3_qa" in ids, str(ds))

    # 全绿 open_pr（离线）
    pr, checks = await gp.open_pr(wf.raw["levels"][1]["nodes"][0], ["n1", "n2"])
    check("全绿 open_pr → merged", pr.state == "merged",
          f"state={pr.state} flags={pr.red_flags}")
    check("全绿 main ff 推进（ff-only 调用在流）",
          any(c[0] == "git" and "merge" in c and "--ff-only" in c for c in fake.calls))


# ============ B) 真 git + 真 venv（tmp 种子仓） ============
def _seed_project(tmp: Path) -> None:
    """种子项目：主仓代码树 + 两个真实门禁脚本（绿/红，无内嵌引号，跨平台）。"""
    (tmp / "README.md").write_text("# company\n", encoding="utf-8")
    (tmp / "backend").mkdir()
    (tmp / "backend" / "app").mkdir()
    (tmp / "backend" / "app" / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp / "backend" / "tests").mkdir()
    (tmp / "backend" / "tests" / "test_ok.py").write_text(
        "def test_green():\n    assert True\n", encoding="utf-8")
    # 门禁脚本（venv cwd=git-sandbox 根下能找到，因 seed 把项目根拷进去）
    (tmp / "check_green.py").write_text(
        "import sys\nprint('check_green: 通过')\nsys.exit(0)\n", encoding="utf-8")
    (tmp / "check_red.py").write_text(
        "import sys\nprint('check_red: 发现 2 处缺陷（模拟门禁失败）')\nsys.exit(1)\n",
        encoding="utf-8")


async def test_real_green():
    print("\n== B-① 全绿（两 Dev 改不同文件 + 绿脚本）→ main 推进 ==")
    tmp = Path(tempfile.mkdtemp(prefix="company-p2g-"))
    _seed_project(tmp)
    wf = make_wf()
    wf.raw["git_policy"] = {"backend": "subprocess", "baseBranch": "main",
                            "checkScripts": ["python check_green.py"],
                            "checkTimeoutSec": 60}
    bus = EventBus()
    evs: list[str] = []
    bus.subscribe(lambda e: evs.append(e.type))
    gp = SubprocessGitPolicy(wf, bus, project_root=tmp)
    await gp.dev_submit(wf.raw["levels"][0]["nodes"][0], {"mod_a.py": "A=1\n"})
    await gp.dev_submit(wf.raw["levels"][0]["nodes"][1], {"mod_b.py": "B=1\n"})
    head0 = await gp.repo.main_head()
    pr, checks = await gp.open_pr(wf.raw["levels"][1]["nodes"][0], ["n1", "n2"])
    script_ok = next(c for c in checks if c["name"].startswith("script:"))
    check("venv 真跑绿脚本 rc=0", script_ok["passed"], script_ok["detail"])
    check("全绿 PR merged", pr.state == "merged", f"flags={pr.red_flags}")
    head1 = await gp.repo.main_head()
    check("main head 物理推进（全绿唯一写主干路径）", head1 != head0,
          f"{head0[:10]}→{head1[:10]}")
    check("merge_sha 可溯源（非空 40 位）", bool(pr.merge_sha) and len(pr.merge_sha) == 40,
          (pr.merge_sha or "")[:12])
    check("种子仓自包含代码树（checkScripts 在影子仓根真跑）",
          gp.repo.has_file("backend/tests/test_ok.py"))
    check("事件流含 pr_merged（大盘数据源）", "pr_merged" in evs, str(sorted(set(evs))))


async def test_real_conflict():
    print("\n== B-② 冲突红（同文件 add/add）→ rejected + main 未动 + node_blocked ==")
    tmp = Path(tempfile.mkdtemp(prefix="company-p2c-"))
    _seed_project(tmp)
    wf = make_wf()
    wf.raw["git_policy"] = {"backend": "subprocess", "baseBranch": "main",
                            "checkScripts": ["python check_green.py"],
                            "checkTimeoutSec": 60}
    bus = EventBus()
    blocked: list[dict] = []
    bus.subscribe(lambda e: blocked.append(e.data) if e.type == "node_blocked" else None)
    gp = SubprocessGitPolicy(wf, bus, project_root=tmp)
    await gp.dev_submit(wf.raw["levels"][0]["nodes"][0], {"shared.py": "x=1\n"})
    await gp.dev_submit(wf.raw["levels"][0]["nodes"][1], {"shared.py": "y=2\n"})
    head0 = await gp.repo.main_head()
    pr, checks = await gp.open_pr(wf.raw["levels"][1]["nodes"][0], ["n1", "n2"])
    conflict_check = next(c for c in checks if c["name"] == "conflict")
    check("冲突门禁亮红灯", not conflict_check["passed"], conflict_check["detail"])
    check("PR rejected（拦截打回）", pr.state == "rejected", str(pr.red_flags))
    check("★ 红线：冲突路径 main 全程未动", await gp.repo.main_head() == head0)
    blocked_ids = sorted({d["node_id"] for d in blocked})
    check("§1.4 node_blocked 发出（pr_gate + 下游 n3_qa）",
          "n2_pr" in blocked_ids and "n3_qa" in blocked_ids, str(blocked_ids))
    check("node_blocked reason=upstream_red",
          all(d["reason"] == "upstream_red" for d in blocked))
    check("下游 n3_qa blocked_by 指向 pr_gate",
          any(d["node_id"] == "n3_qa" and "n2_pr" in d["blocked_by"] for d in blocked))


async def test_real_script_red():
    print("\n== B-③ 脚本红（venv 真跑红脚本）→ rejected + main 未动 ==")
    tmp = Path(tempfile.mkdtemp(prefix="company-p2s-"))
    _seed_project(tmp)
    wf = make_wf()
    wf.raw["git_policy"] = {"backend": "subprocess", "baseBranch": "main",
                            "checkScripts": ["python check_red.py"],
                            "checkTimeoutSec": 60}
    bus = EventBus()
    evs: list[str] = []
    bus.subscribe(lambda e: evs.append(e.type))
    gp = SubprocessGitPolicy(wf, bus, project_root=tmp)
    await gp.dev_submit(wf.raw["levels"][0]["nodes"][0], {"mod_a.py": "A=1\n"})
    head0 = await gp.repo.main_head()
    pr, checks = await gp.open_pr(wf.raw["levels"][1]["nodes"][0], ["n1"])
    script_check = next(c for c in checks if c["name"].startswith("script:"))
    check("venv 真跑红脚本 rc=1 → 门禁红灯", not script_check["passed"],
          script_check["detail"][:60])
    check("PR rejected（脚本红）", pr.state == "rejected", str(pr.red_flags))
    check("★ 红线：脚本红路径 main 全程未动", await gp.repo.main_head() == head0)
    check("shell_log 事件流（venv 子进程日志）", "shell_log" in evs, str(sorted(set(evs))))
    check("§1.4 脚本红同样发 node_blocked（大盘锁链）", "node_blocked" in evs)


# ============ C) 真 Orchestrator 集成（mock Dev + 真 git + 真 pr_gate，复刻 main.py 接线） ============
def _subprocess_wf(check_script: str) -> LoadedWorkflow:
    """两 mock Dev（改不同文件）+ pr_gate + QA，git_policy backend=subprocess。
    绿链 check_script=check_green.py，红链=check_red.py（venv 真跑）。"""
    raw = {
        "meta": {"name": "3b2-integration", "version": "0"},
        "agents": {
            "dev_a": {"id": "dev_a", "role": "DevA", "provider": "mock",
                      "tools": ["write_file"], "outputs": [{"name": "mod_a", "kind": "text"}]},
            "dev_b": {"id": "dev_b", "role": "DevB", "provider": "mock",
                      "tools": ["write_file"], "outputs": [{"name": "mod_b", "kind": "text"}]},
            "reviewer": {"id": "reviewer", "role": "评审", "provider": "mock",
                        "tools": [], "outputs": [{"name": "review", "kind": "text"}]},
        },
        "git_policy": {"backend": "subprocess", "baseBranch": "main",
                       "checkScripts": [f"python {check_script}"], "checkTimeoutSec": 60},
        "levels": [
            {"index": 0, "name": "L0", "nodes": [
                {"id": "n1", "agent": "dev_a", "task": "模块A", "branch": "dev-a"},
                {"id": "n2", "agent": "dev_b", "task": "模块B", "branch": "dev-b"}]},
            {"index": 1, "name": "L1", "nodes": [
                {"id": "n2_pr", "kind": "pr_gate", "agent": "reviewer", "task": "门禁",
                 "inputs": ["n1.mod_a", "n2.mod_b"],
                 "onError": {"policy": "failFast"}}]},
            {"index": 2, "name": "L2", "nodes": [
                {"id": "n3_qa", "kind": "qa", "agent": "reviewer", "task": "验收",
                 "inputs": ["n2_pr.merge_sha"]}]},
        ],
        "qa": {"onFailure": ["console"], "retry": {"maxAttempts": 1}},
    }
    return LoadedWorkflow(raw=raw, workflow_id="3b2-int", path=Path("."))


async def _run_orch(tmp: Path, check_script: str) -> tuple[dict, list[str]]:
    """复刻 main.py 接线：按 backend=subprocess 选 SubprocessGitPolicy，挂进真实 Orchestrator。"""
    from app.engine.orchestrator import Orchestrator
    from app.persistence.progress import ProgressLog
    wf = _subprocess_wf(check_script)
    bus = EventBus()
    evs: list[str] = []
    bus.subscribe(lambda e: evs.append(e.type))
    gp = SubprocessGitPolicy(wf, bus, project_root=tmp)
    orch = Orchestrator(wf, bus, ProgressLog(str(tmp)), checkpoint_root=str(tmp),
                        git_policy=gp)
    res = await orch.run()
    return res, evs


async def test_orchestrator_green():
    print("\n== C-① 真 Orchestrator 集成·全绿（mock Dev → 真 pr_gate → main 推进）==")
    tmp = Path(tempfile.mkdtemp(prefix="company-p2o-"))
    _seed_project(tmp)
    res, evs = await _run_orch(tmp, "check_green.py")
    check("orchestrator 全绿 run success", res["success"], f"failed_level={res['failed_level']}")
    check("QA 节点拿到 merge_sha（数据流贯通）",
          (res["results"].get("n3_qa") or {}).get("ok"), str(res["results"].get("n3_qa")))
    check("事件流含 pr_merged（大盘 git 实时流）", "pr_merged" in evs, str(sorted(set(evs))))
    check("orchestrator 全程真 git（git_commit 走 subprocess backend）",
          "git_commit" in evs)


async def test_orchestrator_red():
    print("\n== C-② 真 Orchestrator 集成·脚本红（venv 真跑红脚本 → run FAILED + main 未动）==")
    tmp = Path(tempfile.mkdtemp(prefix="company-p2r-"))
    _seed_project(tmp)
    wf = _subprocess_wf("check_red.py")
    bus = EventBus()
    evs: list[str] = []
    bus.subscribe(lambda e: evs.append(e.type))
    from app.engine.orchestrator import Orchestrator
    from app.persistence.progress import ProgressLog
    gp = SubprocessGitPolicy(wf, bus, project_root=tmp)
    # main 基准在第一个 pr 前
    await gp.repo.ensure_repo()
    head0 = await gp.repo.main_head()
    orch = Orchestrator(wf, bus, ProgressLog(str(tmp)), checkpoint_root=str(tmp),
                        git_policy=gp)
    res = await orch.run()
    check("orchestrator 脚本红 run FAILED（门禁拦截）", not res["success"],
          f"failed_level={res['failed_level']}")
    check("★ 红线：run 失败后 main 全程未动", await gp.repo.main_head() == head0)
    check("pr_rejected 事件（拦截可溯源）", "pr_rejected" in evs, str(sorted(set(evs))))
    check("§1.4 node_blocked 发出（pr_gate 红灯连锁）", "node_blocked" in evs)


async def main() -> int:
    await test_offline()
    await test_real_green()
    await test_real_conflict()
    await test_real_script_red()
    await test_orchestrator_green()
    await test_orchestrator_red()
    failed = [r for r in results if not r[1]]
    print(f"\n{'='*44}\n总计 {len(results)} 项，通过 {len(results)-len(failed)}，失败 {len(failed)}")
    for name, _, detail in failed:
        print(f"  ✗ {name}: {detail}")
    print("全部 PASS ★ Phase 3b-② 真 git 分支路由 + 门禁 + BLOCKED 传播 验收通过"
          if not failed else "存在失败项")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
