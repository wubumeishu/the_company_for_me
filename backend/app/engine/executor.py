"""
单节点执行器：ReAct 循环的 Phase 1 骨架（provider 可插拔）。

Strict Constraint #2 的打卡埋点就在这里（executor = 真正"执行代码/命令"的位置）：
    run() →  ① progress.node_enter   ← 执行前打卡（时间/已完成/当前动作/下一步）
              ② bus.node_start       ← 事件流：前端节点转"运行中"
              ③ resolve inputs       ← 上游产物注入（QA 拦截的判定依据）
              ④ provider 执行        ← mock / ollama / codex / claude
              ⑤ progress.node_exit   ← 执行后打卡
              ⑥ bus.node_done|node_error

Phase 1 provider 策略：
    mock     → 确定性本地桩（零外部依赖，全链路可离线跑通；task 含 "sim_fail" 时失败，供红框/回退演示）
    其它     → 尚未接真实 LLM，降级为 mock 并打 shell_log 事件明示（保持全链路可跑，不静默换道）
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ..persistence.progress import ProgressLog
from ..schema.validator import LoadedWorkflow
from .events import EventBus


@dataclass
class NodeOutcome:
    """行调度器（orchestrator）消费的单节点结果。"""
    level: int
    node_id: str
    ok: bool
    outputs: dict[str, str] = field(default_factory=dict)   # 产物名 -> 文本（QA 输入来源）
    error: Optional[str] = None


class NodeExecutor:
    def __init__(self, node: dict[str, Any], agent: dict[str, Any], wf: LoadedWorkflow,
                 bus: EventBus, progress: ProgressLog):
        self.node = node
        self.agent = agent
        self.wf = wf
        self.bus = bus
        self.progress = progress
        self.level: int = 0                      # orchestrator 在调度时填
        self.node_id = node["id"]
        self.provider = (agent or {}).get("provider", "mock")
        self.kind = node.get("kind", "agent")
        self.max_turns = int((agent or {}).get("maxTurns", 12))

    # ------------------------------------------------------------------ 入口
    async def run(self, upstream: Optional[dict[str, NodeOutcome]] = None) -> NodeOutcome:
        task = self.node.get("task", "")
        upstream = upstream or {}

        # ① ★ 执行前打卡（Strict Constraint #2）：真正动手之前，先写 progress.md
        self.progress.node_enter(self.level, self.node_id, self.provider,
                                 next_step=f"执行任务: {task[:40]}…")

        # ② 事件：节点进入运行态（前端 AgentNode 转蓝色/转圈）
        await self.bus.node_started(self.level, self.node_id, self.provider)

        # ③ 解析上游输入 "nodeId.outputName" → 产物文本。
        #    缺任何一个 = 数据流断裂：agent 节点直接失败；qa/review 节点拦截（这是红框的常规来源）。
        missing: list[str] = []
        resolved: dict[str, str] = {}
        for ref in self.node.get("inputs", []):
            up_id, _, out_name = ref.partition(".")
            up = upstream.get(up_id)
            if up is None or up.error or out_name not in up.outputs:
                missing.append(ref)
            else:
                resolved[ref] = up.outputs[out_name]

        # ④ provider 执行
        if missing:
            reason = f"上游输入缺失: {missing}（{'QA 拦截' if self.kind in ('qa', 'review') else '数据流断裂'}）"
            outcome = NodeOutcome(self.level, self.node_id, ok=False, error=reason)
        else:
            outcome = await self._execute_provider(resolved)

        # ⑤ ★ 执行后打卡
        self.progress.node_exit(self.level, self.node_id, self.provider,
                                completed=f"节点{'通过' if outcome.ok else '失败'}: {(outcome.error or task)[:40]}")

        # ⑥ 事件：终态（红框由 orchestrator 在 gate 判定后统一触发，这里只报节点级结果）
        if outcome.ok:
            await self.bus.node_finished(self.level, self.node_id, ok=True,
                                         output="; ".join(outcome.outputs.values())[:200])
        else:
            await self.bus.node_finished(self.level, self.node_id, ok=False, output=outcome.error or "")
        return outcome

    # ------------------------------------------------------------- provider 分派
    async def _execute_provider(self, resolved_inputs: dict[str, str]) -> NodeOutcome:
        task = self.node.get("task", "")
        # task 含 "sim_fail" = 确定性失败钩子（演示 QA 拦截 + 红框 + 回退，不需要真 LLM）
        force_fail = "sim_fail" in task

        if self.provider == "mock":
            await self._run_mock(resolved_inputs, fail=force_fail)
        else:
            # Phase 1：真实 LLM 通道（ollama/codex/claude）未接入 → 明示降级，不静默换道
            await self.bus.publish("shell_log", node_id=self.node_id,
                                   cmd=f"$ provider:{self.provider} (未接入, Phase 1 降级为 mock 执行)")
            await self._run_mock(resolved_inputs, fail=force_fail)
            return await self._mock_outcome()  # mock 路径已经把结果存到 _pending

        # QA/Review 类节点：mock 执行即"通过"（拦截逻辑在 orchestrator 的 gate 判定 + 上游输入解析里）
        return await self._mock_outcome()

    async def _mock_outcome(self) -> NodeOutcome:
        return self._pending

    async def _run_mock(self, resolved_inputs: dict[str, str], fail: bool = False) -> None:
        """确定性 mock：模拟 ReAct 两拍（思考 + 工具调用 + 产物），带微小延迟让前端能看到并发。"""
        await self.bus.publish("thinking", node_id=self.node_id,
                               text=f"[mock:{self.provider}] 分析任务: {self.node.get('task', '')[:60]}")
        await asyncio.sleep(0.05)
        await self.bus.publish("tool_call", node_id=self.node_id,
                               tool=self.node.get("toolOverrides") or self.agent.get("tools", [])[:1],
                               args={"inputs": list(resolved_inputs)})
        await asyncio.sleep(0.05)

        declared = [o.get("name", "report") for o in self.agent.get("outputs", [])] or ["report"]
        artifacts = {name: f"[mock artifact] {self.node_id}:{name} 基于上游{list(resolved_inputs) or '无'}"
                       for name in declared}
        if fail:
            self._pending = NodeOutcome(self.level, self.node_id, ok=False,
                                        error=f"sim_fail 钩子: {self.node.get('task', '')[:60]}")
        else:
            self._pending = NodeOutcome(self.level, self.node_id, ok=True, outputs=artifacts)
