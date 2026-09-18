"""
git_policy.py —— Phase 3a：Git 分支隔离 + PR 门禁（ARCHITECTURE_V2 §3 第一道防线）。

设计决策回顾（§3 结论）：
  引擎执行面 = Python 控制（simulated 起步，subprocess 后续）——可测、可回放、错误语义可控；
  仓库守卫面 = 真实 Git Hooks（scripts/git-hooks/，第二道，防人绕过引擎）；
  CI 门禁   = 远端 Actions（.github/workflows/ci.yml，第三道，PR 自动跑测试+不变量）。

本模块负责"引擎权威层"：
  1) 分支隔离：每个 Dev 节点只在 <branch>（缺省=节点 id）上提交，主干引擎绝不直改；
  2) PR 门禁：pr_gate 节点对上游分支跑 merge 检查（冲突 dry-run + 门禁脚本），
     任一红灯 → pr_rejected + 复用 Phase 1 红框/回退三件套；
  3) 全动作发 git_* 事件（站会大盘的 Git 实时流数据源）。

backend=simulated（Phase 3a 默认）：影子仓库 = 内存 commit 图（每分支一条 append-only 链），
  零外部依赖零副作用（与 mock provider 同思路）；"冲突" = 同文件被 ≥2 条分支同时改；
  门禁脚本 = 可注入 checker 函数（默认 style/test 两条代理规则），不真起进程。
backend=subprocess（后续阶段）：asyncio 子进程真 git CLI，接口不变只换实现。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .events import EventBus

# checker 签名：(pr, 上下文) -> (通过?, 原因)。注入式，单测可换规则
Checker = Callable[["PullRequest", dict[str, Any]], tuple[bool, str]]


@dataclass
class Commit:
    branch: str
    node_id: str
    message: str
    files: dict[str, str]          # 文件名 -> 内容（模拟 diff 的 hunk）
    ts: float
    sha: str = ""
    parent: Optional[str] = None


@dataclass
class PullRequest:
    pr_id: str
    title: str
    source_branches: list[str]     # 多 Dev 分支合入主干（敏捷团队并行产物）
    target: str
    state: str = "open"            # open | merged | rejected
    red_flags: list[str] = field(default_factory=list)
    merge_sha: Optional[str] = None  # merged 时的 squash commit（git 证据，QA 可溯源）

    @property
    def is_green(self) -> bool:
        return not self.red_flags


class ShadowRepo:
    """影子仓库：内存 append-only 提交图（simulated backend 的底层）。"""

    def __init__(self, base_branch: str = "main"):
        self.base = base_branch
        self.heads: dict[str, Optional[str]] = {base_branch: None}
        self.commits: dict[str, Commit] = {}
        self._seq = 0

    def commit(self, branch: str, node_id: str, message: str, files: dict[str, str]) -> Commit:
        head = self.heads.get(branch)
        self._seq += 1
        c = Commit(branch, node_id, message, files, time.time(),
                   sha=f"sim-{self._seq:04d}-{branch[:8]}", parent=head)
        self.commits[c.sha] = c
        self.heads[branch] = c.sha
        return c

    def commits_on(self, branch: str) -> list[Commit]:
        out: list[Commit] = []
        cur = self.heads.get(branch)
        while cur:
            c = self.commits[cur]
            out.append(c)
            cur = c.parent
        return list(reversed(out))


class GitPolicy:
    """引擎权威层：分支隔离 + PR 门禁（simulated）。

    用法（executor / pr_gate 节点内）：
        policy = GitPolicy(wf, bus)
        policy.dev_submit(node, artifacts)                 # Dev 完成 → 只在 <branch> 提交
        pr, checks = policy.open_pr(node, ["n1","n2"])    # pr_gate → 冲突+门禁，任一红灯=rejected
    """

    def __init__(self, wf: "LoadedWorkflow", bus: EventBus):
        from ..schema.validator import LoadedWorkflow
        self.wf = wf
        self.bus = bus
        gp = wf.raw.get("git_policy") or {"backend": "simulated"}
        self.backend = gp.get("backend", "simulated")
        self.base = gp.get("baseBranch", "main")
        self.check_scripts: list[str] = list(gp.get("checkScripts", []))
        self.repo = ShadowRepo(self.base)
        self._prs: dict[str, PullRequest] = {}
        self._pr_seq = 0
        self._branch_map: dict[str, str] = {}    # node_id → 实际提交分支（缺省=节点 id）

    # ---------------------------------------------------------------- Dev 分支隔离
    async def dev_submit(self, node: dict[str, Any], artifacts: dict[str, str]) -> str:
        """Dev 节点完成：只在 <branch>（缺省=节点 id）上 append 提交，绝不直改主干。
        返回 commit sha（事件+产物里带证据，QA 拦截必须可溯源）。"""
        branch = node.get("branch") or node["id"]
        self._branch_map[node["id"]] = branch
        c = self.repo.commit(branch, node["id"], f"[{node['id']}] {node.get('task', '')[:40]}",
                             {k: str(v) for k, v in artifacts.items()})
        # 文件名 = 产物名（语义目标文件）：两 Dev 同改一个文件 = 冲突（可区分），改不同文件 = 绿灯
        await self.bus.publish("git_commit", node_id=node["id"], branch=branch,
                               sha=c.sha, msg=c.message, backend=self.backend)
        return c.sha

    # ---------------------------------------------------------------- PR 门禁
    async def open_pr(self, pr_node: dict[str, Any], source_node_ids: list[str],
                      checkers: Optional[list[tuple[str, Checker]]] = None
                      ) -> tuple[PullRequest, list[dict[str, Any]]]:
        """对上游 Dev 分支开 PR：冲突 dry-run + 门禁脚本并行；任一红灯 → rejected。
        返回 (pr, checks[])，checks 供事件流/红框原因定位（哪条门禁亮了红灯）。"""
        self._pr_seq += 1
        pr = PullRequest(pr_id=f"PR-{self._pr_seq:03d}",
                         title=f"{pr_node.get('task', 'merge')} [{pr_node['id']}]",
                         source_branches=[self._branch_map.get(nid, nid) for nid in source_node_ids],
                         target=self.base)
        await self.bus.publish("pr_opened", pr=pr.pr_id, source_branches=pr.source_branches,
                               target=self.base, backend=self.backend)

        checks: list[dict[str, Any]] = []
        # 检查 1：冲突 dry-run（同文件被 ≥2 条分支改 = 近似冲突）
        conflict = self._detect_conflict(pr.source_branches)
        checks.append({"name": "conflict", "passed": not conflict,
                       "detail": f"冲突: {conflict}" if conflict else "无合并冲突"})
        if conflict:
            pr.red_flags.append(f"conflict: {conflict}")

        # 检查 2：门禁脚本（simulated=注入 checker；默认 style/test 两条代理规则）
        for name, fn in (checkers or self._default_checkers()):
            ok, reason = fn(pr, {"sources": pr.source_branches, "scripts": self.check_scripts})
            checks.append({"name": name, "passed": ok, "detail": reason})
            if not ok:
                pr.red_flags.append(f"{name}: {reason}")

        # 终态：全绿 → squash 合入主干（引擎唯一可写主干的合法路径）；红灯 → 拦截打回
        if pr.is_green:
            pr.state = "merged"
            pr.merge_sha = self._squash_into_base(pr)
            await self.bus.publish("pr_merged", pr=pr.pr_id, sha=pr.merge_sha,
                                   source_branches=pr.source_branches, backend=self.backend)
        else:
            pr.state = "rejected"
            await self.bus.publish("pr_rejected", pr=pr.pr_id, red_flags=pr.red_flags,
                                   checks=checks, backend=self.backend)
        self._prs[pr.pr_id] = pr
        return pr, checks

    # ---------------------------------------------------------------- 内部
    def _detect_conflict(self, branches: list[str]) -> str:
        """同文件被 ≥2 条【不同】分支修改 = 近似冲突（simulated 的同键 hunk 模型）。"""
        by_file: dict[str, set[str]] = {}
        for b in branches:
            for c in self.repo.commits_on(b):
                for fname in c.files:
                    by_file.setdefault(fname, set()).add(b)
        for fname, bs in by_file.items():
            if len(bs) > 1:
                return f"{fname} 被 {sorted(bs)} 同时修改"
        return ""

    def _squash_into_base(self, pr: PullRequest) -> str:
        files: dict[str, str] = {}
        for b in pr.source_branches:
            for c in self.repo.commits_on(b):
                files.update(c.files)
        msg = f"merge {pr.pr_id}: {pr.title} (squash of {len(pr.source_branches)} branches)"
        return self.repo.commit(self.base, pr.pr_id, msg, files).sha

    def _default_checkers(self) -> list[tuple[str, Checker]]:
        """simulated 内置门禁（不真起进程；CI 才是真门禁，这里是引擎本地拦截代理）：
        style = 产物非空（空提交被拒）；test = 每个源分支至少一个提交（产物存在性）。"""

        def style(pr: PullRequest, ctx: dict[str, Any]) -> tuple[bool, str]:
            total = sum(len(c.files) for b in pr.source_branches
                        for c in self.repo.commits_on(b))
            if total == 0:
                return False, "无产物（style 规则：空提交被拒）"
            return True, f"{total} 个产物通过风格检查"

        def test(pr: PullRequest, ctx: dict[str, Any]) -> tuple[bool, str]:
            for b in pr.source_branches:
                if not self.repo.commits_on(b):
                    return False, f"分支 {b} 无提交（test 门禁缺产物）"
            return True, "全部分支产物齐备（test 门禁代理通过）"

        return [("style", style), ("test", test)]
