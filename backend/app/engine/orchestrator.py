"""
行调度引擎（Task 1 核心）：把 levels[] 变成"行内真并发 + 行间 Barrier"。

执行模型（严格对应三条约束）：
  for level in levels:
      ┌─ 行内：asyncio.gather(*[executor.run(node) ...])   ← 真并发（同一行同 Y 坐标的节点并行跑）
      ├─ Barrier：gather 返回 = 该行全部节点结束（kudosflow 行级 barrier）
      ├─ gate 判定：all → 任一失败即拦；any → 多数成功放行
      └─ QA/Review 拦截：kind∈{qa,review} 的节点失败 = 该行为 FAIL，
         触发三件套：console（EventBus 自动镜像 stderr）+ 红框（red_highlight）+ 定向回退

回退语义（Strict Constraint #3）：
  - QA 行失败 → rollback 到 targetLevel（全局 qa.rollback 或节点级 onError.rollbackToLevel）
  - checkpoint.keepCheckpoint=True 时：已通过的更上游行不重跑，只重跑目标行（防死循环：同目标行最多回退 max_rollback 次）
  - 回退失败即 run_finish(success=False)
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from ..persistence.checkpoint import CheckpointStore, NodeResult
from ..persistence.progress import ProgressLog
from ..schema.validator import LoadedWorkflow
from .events import EventBus
from .executor import NodeExecutor, NodeOutcome


class Orchestrator:
    """一个 workflow 的整轮执行器（run 维度，每个 run 独占一个 EventBus）。"""

    def __init__(self, wf: LoadedWorkflow, bus: EventBus, progress: ProgressLog,
                 checkpoint_root: Optional[str] = None):
        self.wf = wf
        self.bus = bus
        self.progress = progress
        # checkpoint 落盘目录：优先显式 root（/run 传 project_root），否则用 meta.projectRoot
        self.checkpoint = CheckpointStore(project_root=checkpoint_root or str(wf.path), run_id=bus.run_id)
        self._retry_count = 0

    # ------------------------------------------------------------------ run
    async def run(self, rollback_target: Optional[int] = None) -> dict[str, Any]:
        """整轮执行入口。rollback_target 非空 = 带回退重跑。"""
        levels = self.wf.levels
        t0 = time.time()
        await self.bus.publish("run_start", workflow=self.wf.workflow_id, levels=len(levels),
                               resuming_from=rollback_target)

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
        prev_outcomes: list[NodeOutcome] = []   # 上一行结果（本行 inputs 的解析来源）

        # ★ 回退上游注入：目标行正上方那行的已通过产物 → 本行 inputs 的来源。
        # （不注入的话回退后目标行节点会因"上游输入缺失"被 QA 拦截，白重跑）
        if rollback_target is not None:
            for v in prev.values():
                if v.level == rollback_target - 1 and v.ok:
                    prev_outcomes.append(NodeOutcome(v.level, v.node_id, True, v.outputs, None))

        for level in levels:
            idx = level["index"]
            if idx < start_level:
                continue                      # 回退重跑时跳过已通过的上游行

            await self.bus.publish("level_start", level=idx, name=level.get("name", ""))
            # 执行前打卡：整行进入（Strict Constraint #2 的行级埋点）
            if self.wf.raw.get("qa", {}).get("rollback", {}).get("notifyProgress", True):
                self.progress.register(f"L{idx}", f"上行 L{idx-1 if idx else '-'} 完成" if idx else "run 启动",
                                       "并发执行本行节点", "本行 Barrier + QA 判定")

            # ---- ★ 行内真并发：asyncio.gather 一把梭 ----
            # 每个节点 = 独立协程（自己的 ReAct 循环 + provider 会话），互不等待
            upstream = {o.node_id: o for o in prev_outcomes}          # 上一行结果 → 本行 inputs 来源
            node_tasks = [self._run_node(level, nd, upstream) for nd in level["nodes"]]
            row_results: list[NodeOutcome] = await asyncio.gather(*node_tasks, return_exceptions=False)
            prev_outcomes = row_results

            # ---- ★ Barrier：gather 已保证整行结束（kudosflow 行级 barrier）----
            ok_nodes = [r for r in row_results if r.ok]
            qa_nodes = [nd for nd in level["nodes"] if nd.get("kind") in ("qa", "review", "gate")]
            row_ok = self._gate_passes(level.get("gate", "all"), row_results, qa_nodes)

            for nd, r in zip(level["nodes"], row_results):
                results[nd["id"]] = NodeResult(idx, nd["id"], r.ok, r.outputs, r.error, time.time())
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
                # 回退事件同样打卡（notifyProgress=True 是默认）
                self.progress.register("qa/rollback", f"L{failed_level} 行未通过",
                                       f"回退到 L{tgt} 定向重跑（checkpoint 保留）", "重跑目标行后回到 Barrier")
                return await self.run(rollback_target=tgt)

        await self.bus.publish("run_finish", success=success,
                               failed_level=failed_level, seconds=round(time.time() - t0, 2),
                               checkpoint=str(self.checkpoint.path))
        return {"success": success, "run_id": self.bus.run_id, "failed_level": failed_level,
                "checkpoint": str(self.checkpoint.path),
                "results": {k: {"ok": v.ok, "error": v.error} for k, v in results.items()}}

    # ------------------------------------------------------------- 单节点执行
    async def _run_node(self, level: dict[str, Any], nd: dict[str, Any],
                        upstream: dict[str, "NodeOutcome"]) -> NodeOutcome:
        agent_ref = nd.get("agent")
        agent = self.wf.agents[agent_ref] if agent_ref else {}
        executor = NodeExecutor(node=nd, agent=agent, wf=self.wf, bus=self.bus,
                                progress=self.progress)
        executor.level = level["index"]       # 行号注入（打卡与事件定位都要）
        return await executor.run(upstream)

    # ------------------------------------------------------------- gate 判定
    @staticmethod
    def _gate_passes(gate: str, row_results: list[NodeOutcome], qa_nodes: list[dict[str, Any]]) -> bool:
        """
        gate=all（默认）：行内全部节点 ok 才算过。QA 节点是"拦截判定器"——
        它自身跑完且 ok=True 才放行（它的失败 = 拦截住，整行 FAIL）。
        gate=any（quorum 行）：多数 ok 即过（容忍局部失败继续下行）。
        """
        if gate == "any":
            return sum(1 for r in row_results if r.ok) * 2 > len(row_results)
        # all：每个节点都必须 ok（含 QA 拦截判定）
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
