"""
行调度引擎（Phase 1 行并发 + Phase 2 资源门/敏捷小队）。

执行模型（严格对应三条约束 + V2 蓝图）：
  for level in levels:
      ┌─ 行内：asyncio.gather(*[...]) 真并发（同一行同 Y 坐标节点并行跑）
      │     · kind=squad 节点 → SquadExecutor（敏捷小队横抽，共享会议室状态，自动解散归建）
      │     · 其余节点        → NodeExecutor（执行前过 ResourceManager 配额门）
      ├─ ★ V2 挂起等待：行内有 RESTING 挂起节点（API 冷却）→ 整行挂起，
      │     tick 心跳检 RESTING 员工是否恢复体力，恢复后自动唤醒重跑挂起节点
      ├─ Barrier：gather + 挂起重跑完成 = 该行全部结束（kudosflow 行级 barrier）
      └─ QA/Review 拦截三件套：console + 红框 + 定向回退（Phase 1 语义不变）

生命周期：run() 内启动 TickDriver（meta.tickIntervalSec，默认 0.5s，可 1s），
run 结束/回退时 stop。Tick 每拍：唤醒到期 RESTING 员工 + 低频发 tick 快照（WS 大盘数据源）。

回退语义（Strict Constraint #3，不变）：
  行 FAIL → rollback 到 targetLevel（全局 qa.rollback 或节点级 onError.rollbackToLevel），
  checkpoint.keepCheckpoint 保留上行产物，重试 ≤ qa.retry.maxAttempts 防死循环。
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

from ..persistence.checkpoint import CheckpointStore, NodeResult
from ..persistence.progress import ProgressLog
from ..schema.validator import LoadedWorkflow
from .events import EventBus
from .executor import NodeExecutor, NodeOutcome, UpstreamIndex
from .provider_registry import Provider
from .resource_manager import ResourceManager, TickDriver
from .squad import SquadExecutor, node_is_squad
from .git_policy import GitPolicy

# 挂起等待时的单次 tick 间隔（秒）：生产真睡，单测注入假 sleep
PumpSleep = Callable[[float], Awaitable[None]]


class Orchestrator:
    """一个 workflow 的整轮执行器（run 维度，每个 run 独占一个 EventBus）。"""

    def __init__(self, wf: LoadedWorkflow, bus: EventBus, progress: ProgressLog,
                 checkpoint_root: Optional[str] = None,
                 rm: Optional[ResourceManager] = None,
                 clock: Optional[Callable[[], float]] = None,
                 sleep: Optional[PumpSleep] = None,
                 git_policy: Optional[GitPolicy] = None,
                 llm_registry: Optional[list[Provider]] = None):
        self.wf = wf
        self.bus = bus
        self.progress = progress
        self.rm = rm
        self.git_policy = git_policy
        self.llm_registry = llm_registry or []            # Phase 2c：providers.json 快照（run 维度，热切换不重起服务）
        self._sleep: PumpSleep = sleep or (lambda d: asyncio.sleep(d))
        self._checkpoint_root = checkpoint_root
        self.checkpoint = CheckpointStore(project_root=checkpoint_root or str(wf.path), run_id=bus.run_id)
        self._retry_count = 0
        self._clock = clock

    def _now(self) -> float:
        import time
        return self._clock() if self._clock else time.time()

    # ------------------------------------------------------------------ run
    async def run(self, rollback_target: Optional[int] = None) -> dict[str, Any]:
        """整轮执行入口。rollback_target 非空 = 带回退重跑。"""
        t0 = self._now()

        # ★ V2：Tick 心跳（随 run 起停；大盘低频快照 + RESTING 唤醒）
        driver: Optional[TickDriver] = None
        if self.rm is not None:
            interval = float(self.wf.raw.get("meta", {}).get("tickIntervalSec", 0.5))
            driver = TickDriver(self.rm, interval=interval)
            await driver.start()
        try:
            return await self._run_pass(rollback_target, t0)
        finally:
            if driver is not None:
                await driver.stop()

    async def _run_pass(self, rollback_target: Optional[int], t0: float) -> dict[str, Any]:
        """单趟执行（回退时按 target 重入）。每趟发一次 run_start（resuming_from 标识本趟）。"""
        levels = self.wf.levels
        await self.bus.publish("run_start", workflow=self.wf.workflow_id, levels=len(levels),
                               resuming_from=rollback_target, v2=(self.rm is not None))

        # 回退重跑：从 checkpoint 拿已通过结果，跳过目标行以上（keepCheckpoint 语义）
        prev: dict[str, NodeResult] = {}
        if rollback_target is not None:
            ck = self.checkpoint.load()
            if ck:
                prev = {k: NodeResult(v["level"], v["node_id"], v["ok"],
                                      outputs=v.get("outputs", {}), error=v.get("error"),
                                      finished_at=v.get("finished_at", 0.0))
                        for k, v in ck.get("nodes", {}).items()}

        results: dict[str, NodeResult] = dict(prev)
        qa_cfg = self.wf.raw.get("qa", {})
        max_rollback = qa_cfg.get("retry", {}).get("maxAttempts", 2)

        start_level = rollback_target if rollback_target is not None else 0
        ran: dict[int, list[NodeResult]] = {}
        failed_level: Optional[int] = None
        prev_outcomes: dict[str, NodeOutcome] = {}

        # ★ 回退上游注入：目标行正上方那行的已通过产物 → 本行 inputs 的来源
        if rollback_target is not None:
            for v in prev.values():
                if v.level == rollback_target - 1 and v.ok:
                    prev_outcomes[v.node_id] = NodeOutcome(v.level, v.node_id, True, v.outputs, None)

        for level in levels:
            idx = level["index"]
            if idx < start_level:
                continue                      # 回退重跑时跳过已通过的上游行

            await self.bus.publish("level_start", level=idx, name=level.get("name", ""))
            if self.wf.raw.get("qa", {}).get("rollback", {}).get("notifyProgress", True):
                self.progress.register(f"L{idx}", f"上行 L{idx-1 if idx else '-'} 完成" if idx else "run 启动",
                                       "并发执行本行节点", "本行 Barrier + QA 判定")

            # ---- ★ 行内真并发：asyncio.gather 一把梭 ----
            upstream_idx = UpstreamIndex(prev_outcomes)
            node_tasks = [self._run_node(level, nd, upstream_idx)
                          for nd in level["nodes"]]
            row_results: list[NodeOutcome] = await asyncio.gather(*node_tasks, return_exceptions=False)

            # ---- ★ V2 挂起等待：RESTING 节点整行挂起，tick 唤醒后重跑（不是失败！）----
            row_results = await self._resume_blocked(idx, level, row_results, upstream_idx)

            prev_outcomes.update({o.node_id: o for o in row_results})

            # ---- ★ Barrier：整行（含挂起重跑）结束 → gate 判定 ----
            ok_nodes = [r for r in row_results if r.ok]
            row_ok = self._gate_passes(level.get("gate", "all"), row_results)

            for nd, r in zip(level["nodes"], row_results):
                results[nd["id"]] = NodeResult(idx, nd["id"], r.ok, r.outputs, r.error, self._now())
            ran[idx] = [results[nd["id"]] for nd in level["nodes"]]

            if row_ok:
                await self.bus.publish("level_done", level=idx, ok=len(ok_nodes), total=len(row_results))
                self._save_checkpoint(results)
            else:
                # ---- ★ QA 拦截三件套（Strict Constraint #3）----
                for nd, r in zip(level["nodes"], row_results):
                    if not r.ok:
                        await self.bus.red_highlight(idx, nd["id"], r.error or "行未通过")
                failed_level = idx
                break

        # ---------------------------------------------------------------- 回退决策
        success = failed_level is None
        if not success:
            tgt = self._resolve_rollback_target(failed_level, ran)
            if tgt is not None and self._retry_count < max_rollback:
                self._retry_count += 1
                await self.bus.publish("rollback", from_level=failed_level, to_level=tgt,
                                       keepCheckpoint=True, attempt=self._retry_count)
                self.progress.register("qa/rollback", f"L{failed_level} 行未通过",
                                       f"回退到 L{tgt} 定向重跑（checkpoint 保留）", "重跑目标行后回到 Barrier")
                return await self._run_pass(rollback_target=tgt, t0=t0)

        await self.bus.publish("run_finish", success=success,
                               failed_level=failed_level, seconds=round(self._now() - t0, 2),
                               checkpoint=str(self.checkpoint.path))
        return {"success": success, "run_id": self.bus.run_id, "failed_level": failed_level,
                "checkpoint": str(self.checkpoint.path),
                "results": {k: {"ok": v.ok, "error": v.error} for k, v in results.items()}}

    # ----------------------------------------------------- V2 挂起等待（冷却即休息）
    async def _resume_blocked(self, idx: int, level: dict[str, Any],
                              row_results: list[NodeOutcome],
                              upstream: UpstreamIndex) -> list[NodeOutcome]:
        """行内有 RESTING 挂起（blocked=True）的节点 → 整行挂起等待资源恢复，tick 唤醒后重跑。

        语义（任务 2）：触达速率限制时该 Agent 暂停所有分配，直到周期重置；
        等待期间 Tick 心跳检查 RESTING 员工是否恢复体力（rm.tick 自动唤醒）。
        防死循环：同节点最多重挂起 3 次（3 个冷却周期内没等到恢复 = 真失败）。
        """
        out = list(row_results)
        for i in range(3):
            blocked = [r for r in out if r.blocked and r.resume_at]
            if not blocked:
                return out
            target = max(r.resume_at for r in blocked)
            await self.bus.publish("row_suspended", level=idx,
                                   nodes=[r.node_id for r in blocked], resume_at=target)
            # tick 驱动等待：每次 tick 检 RESTING 唤醒；生产真睡 0.5s，单测注入假 sleep 快进
            await self._pump_until(target)
            # 重跑挂起节点（等待期间已放行；其余节点不动）
            rerun = {}
            for nd, r in zip(level["nodes"], out):
                if r.blocked and r.resume_at:
                    rerun[nd["id"]] = await self._run_node(level, nd, upstream)
            out = [rerun.get(nd["id"], r) for nd, r in zip(level["nodes"], out)]
        return out

    async def _pump_until(self, target: float) -> None:
        """等待到 target：每拍先 tick（唤醒 RESTING）再判边界。单测注入 clock/sleep 快进。
        顺序铁律：tick 在前 —— 边界时刻（now==target）也要跑一拍，rest_over 事件必发。"""
        while True:
            now = self._now()
            if self.rm is not None:
                await self.rm.tick(now)          # 本拍唤醒到期员工（含边界拍）
            if now >= target:
                return
            await self._sleep(min(0.5, target - now))

    # ------------------------------------------------------------- 单节点执行
    async def _run_node(self, level: dict[str, Any], nd: dict[str, Any],
                        upstream: UpstreamIndex) -> NodeOutcome:
        # ★ V2：敏捷小队节点 → 横向抽调 + 共享会议室 + 自动解散
        if node_is_squad(nd):
            squad_ex = SquadExecutor(self.wf, self.bus, self.progress, self.rm)
            return await squad_ex.run(nd, level["index"], upstream.outcomes)

        agent_ref = nd.get("agent")
        # ★ Phase 2b：继承视图（个人 ∪ 部门：tools 并集 / boundaries 收紧）——executor 消费的是合并后员工
        agent = self.wf.resolved_agent(agent_ref) if agent_ref else {}
        executor = NodeExecutor(node=nd, agent=agent, wf=self.wf, bus=self.bus,
                                progress=self.progress, resource_manager=self.rm,
                                git_policy=self.git_policy,
                                llm_registry=self.llm_registry)
        executor.level = level["index"]       # 行号注入（打卡与事件定位都要）
        return await executor.run(upstream)

    # ------------------------------------------------------------- gate 判定
    @staticmethod
    def _gate_passes(gate: str, row_results: list[NodeOutcome]) -> bool:
        """
        gate=all（默认）：行内全部节点 ok 才算过。QA 节点是"拦截判定器"。
        gate=any（quorum 行）：多数 ok 即过（容忍局部失败继续下行）。
        注意：blocked（RESTING 挂起）的节点此时已被 _resume_blocked 重跑，不会漏到判定。
        """
        if gate == "any":
            return sum(1 for r in row_results if r.ok) * 2 > len(row_results)
        return all(r.ok for r in row_results)

    def _resolve_rollback_target(self, failed_level: int, ran: dict[int, list[NodeResult]]) -> Optional[int]:
        """节点级 onError.rollbackToLevel > 全局 qa.rollback.targetLevel > None(直接失败)。"""
        nds = [n for lvl in self.wf.levels if lvl["index"] == failed_level for n in lvl["nodes"]]
        for nd in nds:
            override = (nd.get("onError") or {}).get("rollbackToLevel")
            if override is not None:
                return override
        return self.wf.raw.get("qa", {}).get("rollback", {}).get("targetLevel")

    def _save_checkpoint(self, results: dict[str, NodeResult]) -> None:
        try:
            self.checkpoint.save({k: v for k, v in results.items() if v.ok})
        except OSError:
            pass  # checkpoint 是优化项，写盘失败不阻塞主流程
