"""
subprocess_git_policy.py —— Phase 3b-②：真 git 分支路由 + 门禁策略
（PHASE_3_ENGINEERING_DEFENSE §2.2 数据流 + §7 拍板记录 + §1.4 Blocked 传播预埋）。

与 3a GitPolicy（simulated）接口 1:1 兼容：dev_submit(node, artifacts) / open_pr(pr_node,
source_ids, checkers?) 签名与返回 (PullRequest, checks[]) 完全一致，main.py 一处按
backend=="subprocess" 切类，executor / pr_gate / orchestrator 零改动。

真实语义（对照 simulated 的近似模型）：
  - dev_submit：Dev 产物写进自己的 worktree（选项 A 物理工位）→ 只在 <branch> commit；
  - open_pr：preview 临时分支（main head 拉出）→ 顺序 merge --squash 各源分支
    （git 本体冲突检测）→ checkScripts 真跑（venv 默认档，拍板 #2）→
      全绿：squash commit + main ff-only 推进（引擎唯一合法写主干路径，物理保证）
      红灯：preview 删除 + main 毫发未动（reset 清场，不变量实测）
  - 冲突 = 真 git 冲突（simulated 的"同键近似"退役）；
  - checkScripts 在 venv 隔离运行时执行（层③），非零 rc / 超时 = 门禁红灯（可溯源）。

§1.4 预埋（不改变现有调度语义，只发事件供 3c 大盘消费）：
  PR 被拒 → block_downstream 沿 inputs 反向图 BFS 出全部下游节点，逐发 node_blocked
  （琥珀锁链描边数据源）；上游转绿重跑 pr 时，3c 大盘据 node_done 自动解锁（BLOCKED
  是派生态，无需人工清——架构书 §1.4 ③ 不变量）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .events import EventBus
from .git_policy import Checker, PullRequest
from .subprocess_sandbox import SandboxGitError, SandboxRepo
from .sop_contract import SopGateError, gate_dev_submit, parse_handoff, build_commit_message
from ..schema.validator import LoadedWorkflow


def downstream_of(wf: LoadedWorkflow, blocked: list[str]) -> list[tuple[int, str]]:
    """纯函数：从 blocked 节点集沿 levels.inputs 反向图 BFS，返回全部（含自身）下游。

    返回 [(level_index, node_id), ...]（BFS 序，去重）。§1.4 Stack-Roadmap 解锁语义：
    上游红灯 = 下游整条锁住；引擎权威，不丢给前端判断。"""
    all_nodes: list[tuple[int, dict[str, Any]]] = []
    for lvl in wf.levels:
        for nd in lvl["nodes"]:
            all_nodes.append((lvl["index"], nd))
    # 正向边：upstream_id -> 依赖它的下游 node_id
    dependents: dict[str, list[str]] = {}
    for _idx, nd in all_nodes:
        for ref in nd.get("inputs", []):
            up = ref.partition(".")[0]
            if up and up != nd["id"]:
                dependents.setdefault(up, []).append(nd["id"])

    out: list[tuple[int, str]] = []
    seen: set[str] = set()
    frontier = list(blocked)
    while frontier:
        cur = frontier.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        out.append(_find_node(all_nodes, cur))
        frontier.extend(dependents.get(cur, []))
    return out


def _find_node(all_nodes: list[tuple[int, dict[str, Any]]], node_id: str) -> tuple[int, str]:
    for idx, nd in all_nodes:
        if nd["id"] == node_id:
            return idx, node_id
    return (-1, node_id)


class SubprocessGitPolicy:
    """引擎权威层（subprocess backend）：真 git 分支隔离 + PR 门禁 + 状态传播预埋。

    用法与 GitPolicy 相同：
        policy = SubprocessGitPolicy(wf, bus, project_root=tmp_or_repo_root)
        await policy.dev_submit(node, artifacts)
        pr, checks = await policy.open_pr(pr_node, ["n1", "n2"])
    """

    def __init__(self, wf: LoadedWorkflow, bus: EventBus,
                 project_root: Optional[str | Path] = None,
                 repo: Optional[SandboxRepo] = None):
        from .provider_registry import REPO_ROOT
        self.wf = wf
        self.bus = bus
        gp = wf.raw.get("git_policy") or {}
        self.backend = "subprocess"
        self.base = gp.get("baseBranch", "main")
        self.check_scripts: list[str] = list(gp.get("checkScripts", []))
        self.check_timeout = float(gp.get("checkTimeoutSec", 300))
        self.exec_sandbox = gp.get("execSandbox", "venv")   # docker 档由 3b-③ 接入
        # 拍板 #1：影子仓位置 .company/git-sandbox（默认相对 project_root；repoPath 可覆盖）
        repo_path = gp.get("repoPath", ".company/git-sandbox")
        p = Path(repo_path)
        sandbox_root = p if p.is_absolute() else (Path(project_root or REPO_ROOT) / p)
        seed_root = Path(project_root or REPO_ROOT)     # 主仓代码树 = checkScripts 真跑的基准
        self.repo = repo or SandboxRepo(
            sandbox_root, base_branch=self.base, bus=bus,
            trusted=bool(gp.get("trusted", False)),
            timeout=float(gp.get("gitTimeoutSec", 120)),
            seed_from=seed_root)
        # 拍板 #4 强契约：git_policy.sopContract=true 时，dev_submit 强制校验 dev_handoff
        self.sop_contract = bool(gp.get("sopContract", False))
        # repo 暴露 = 与 GitPolicy.repo（ShadowRepo）同名的属性位（测试/诊断用）
        self._preview_seq = 0
        self._prs: dict[str, PullRequest] = {}

    # ---------------------------------------------------------------- Dev 分支隔离
    async def dev_submit(self, node: dict[str, Any], artifacts: dict[str, str]) -> str:
        """Dev 节点完成：产物写进自己 worktree（物理工位），只在 <branch> commit。
        返回 commit sha（QA 拦截可溯源）。幂等：同节点重跑（纠错重试）= 叠在原分支上。

        ★ 3b-③ 强契约（拍板 #4）：sop_contract 开启时，dev_handoff 不合法 → 抛 SopGateError
        （executor 捕获 → 节点 FAIL + 不触发 git + 发 sop_fail），残缺/口头交接物理不进仓。"""
        nid = node["id"]
        if self.sop_contract:
            gate_dev_submit(nid, artifacts)   # 不合法 → SopGateError（不进 git）
        branch = node.get("branch") or nid
        await self.repo.add_worktree(nid, branch)
        # commit msg：有合法 dev_handoff 用规范说明，否则回落 node task（sop 关闭档）
        if self.sop_contract:
            _hok, _hwhy, handoff = parse_handoff(artifacts.get("dev_handoff"))
            msg = build_commit_message(handoff, nid)
        else:
            msg = f"{nid}: {str(node.get('task', ''))[:40]}"
        sha = await self.repo.commit_in_worktree(nid, msg, {k: str(v) for k, v in artifacts.items()})
        await self.bus.publish("git_commit", node_id=nid, branch=self.repo.branch_for(nid),
                               sha=sha, msg=msg, backend=self.backend)
        return sha

    # ---------------------------------------------------------------- PR 门禁
    async def open_pr(self, pr_node: dict[str, Any], source_node_ids: list[str],
                      checkers: Optional[list[tuple[str, Checker]]] = None
                      ) -> tuple[PullRequest, list[dict[str, Any]]]:
        """对上游 Dev 分支开 PR：preview 分支叠加 squash → checkScripts 真跑（venv）。
        全绿 → main ff 推进（引擎唯一合法写主干）；任一红灯 → 拦截打回 + main 未动。"""
        await self.repo.ensure_repo()
        self._preview_seq += 1
        pr = PullRequest(pr_id=f"PR-{self._preview_seq:03d}",
                         title=f"{pr_node.get('task', 'merge')} [{pr_node['id']}] @ {self.backend}",
                         source_branches=[self._branch_of(nid) for nid in source_node_ids],
                         target=self.base)
        main_head_before = await self.repo.main_head()
        preview = f"pr-preview-{pr.pr_id}"
        await self.repo.delete_branch(preview)
        await self.repo.branch(preview, "HEAD")
        await self.repo.checkout(preview)
        await self.bus.publish("pr_opened", pr=pr.pr_id, source_branches=pr.source_branches,
                               target=self.base, backend=self.backend)

        checks: list[dict[str, Any]] = []
        # 检查 ① 冲突：顺序 merge --squash 各源分支（git 本体检测，非近似模型）
        conflict = ""
        for nid in source_node_ids:
            src = self._branch_of(nid)
            if not await self.repo.stage_squash(nid):
                conflict = f"{src} 与当前暂存区冲突（真 git 检测）"
                break
        checks.append({"name": "conflict", "passed": not conflict,
                       "detail": f"冲突: {conflict}" if conflict else "无合并冲突"})
        if conflict:
            pr.red_flags.append(f"conflict: {conflict}")

        # 检查 ② 门禁脚本真跑（venv 默认档；conflict 红灯时跳过——根因已明，省 venv）
        if not conflict:
            for script in self.check_scripts:
                ok, detail = await self._run_check_script(script, pr)
                checks.append({"name": f"script:{script[:40]}", "passed": ok, "detail": detail})
                if not ok:
                    pr.red_flags.append(f"script[{script[:40]}]: {detail}")
        else:
            for script in self.check_scripts:
                checks.append({"name": f"script:{script[:40]}", "passed": False,
                               "detail": "跳过（conflict 拦截）"})

        # 检查 ③ 注入 checker（与 simulated 相同的注入位，3b-③ SOP Gate 挂这里）
        if checkers:
            for name, fn in checkers:
                ok, reason = fn(pr, {"sources": pr.source_branches,
                                     "scripts": self.check_scripts})
                checks.append({"name": name, "passed": ok, "detail": reason})
                if not ok:
                    pr.red_flags.append(f"{name}: {reason}")

        # 终态：全绿 = squash commit + main ff 推进；红灯 = 清场 + 拦截打回（main 未动）
        if pr.is_green:
            pr.state = "merged"
            pr.merge_sha = main_head_before          # 空合入（无 staged）= 无操作
            if await self.repo.has_staged():
                sha = await self.repo.commit_squash(f"merge {pr.pr_id}: {pr.title}")
                if sha:
                    pr.merge_sha = sha
            await self.repo.checkout(self.base)
            await self.repo.merge_ff(preview)        # ff-only：main 只在全绿时物理前进
            await self.bus.publish("pr_merged", pr=pr.pr_id, sha=pr.merge_sha,
                                   source_branches=pr.source_branches, backend=self.backend)
        else:
            pr.state = "rejected"
            await self.repo.discard_squash()          # 清 preview 的 index/工作区（冲突标记残留）
            await self.repo.checkout(self.base)       # main 工作区恢复 HEAD 原状
            await self.bus.publish("pr_rejected", pr=pr.pr_id, red_flags=pr.red_flags,
                                   checks=checks, backend=self.backend)
            # §1.4 预埋：PR 被拒 → 该节点 + 全部下游节点 Blocked（大盘锁链描边数据源）
            await self._block_downstream(pr_node, source_node_ids)

        await self.repo.delete_branch(preview)
        self._prs[pr.pr_id] = pr
        return pr, checks

    # ---------------------------------------------------------------- 内部
    def _branch_of(self, node_id: str) -> str:
        return self.repo.branch_for(node_id)

    async def _run_check_script(self, script: str, pr: PullRequest) -> tuple[bool, str]:
        """venv 隔离真跑 checkScript（层③，拍板 #2）。超时/非零 rc = 红灯（带日志尾可溯源）。"""
        if self.exec_sandbox == "docker":
            raise SandboxGitError("execSandbox=docker 档由 3b-③ 接入（本阶段 venv 默认档）")
        res = await self.repo.run_shell_in_venv(
            script, node_id=pr.pr_id, check_timeout=self.check_timeout)
        if res.timed_out:
            return False, f"超时（>{self.check_timeout:.0f}s）"
        if res.returncode != 0:
            tail = (res.stderr or res.stdout or "").strip()[-300:]
            return False, f"rc={res.returncode}: {tail}"
        return True, f"rc=0（{script[:40]} 通过）"

    async def _block_downstream(self, pr_node: dict[str, Any],
                                source_node_ids: list[str]) -> None:
        """§1.4 状态传播预埋：pr_gate 红灯 → 该节点 + 全部下游（BFS）逐发 node_blocked。
        区别于"失败红框"：BLOCKED = 压根没资格调度（上游红灯连锁）。BLOCKED 是派生态——
        上游 pr 重跑转绿后，3c 大盘据 node_done 自动解锁（§1.4 ③ 不变量，无需人工清）。"""
        lvl = _find_node([(l["index"], n) for l in self.wf.levels for n in l["nodes"]],
                         pr_node["id"])[0]
        await self.bus.publish("node_blocked", level=lvl, node_id=pr_node["id"],
                               reason="upstream_red", blocked_by=source_node_ids,
                               gate=pr_node.get("kind", "pr_gate"))
        for idx, nid in downstream_of(self.wf, [pr_node["id"]]):
            if nid == pr_node["id"]:
                continue
            await self.bus.publish("node_blocked", level=idx, node_id=nid,
                                   reason="upstream_red", blocked_by=[pr_node["id"]])
