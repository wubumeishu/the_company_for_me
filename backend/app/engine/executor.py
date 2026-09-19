"""
单节点执行器：ReAct 循环骨架（provider 可插拔）+ V2 资源门。

Strict Constraint #2 的打卡埋点就在这里（executor = 真正"执行代码/命令"的位置）：
    run() →  ① progress.node_enter   ← 执行前打卡（时间/已完成/当前动作/下一步）
              ② bus.node_start       ← 事件流：前端节点转"运行中"
              ③ resolve inputs       ← 上游产物注入（QA 拦截的判定依据）
              ④ ★ 资源门 acquire     ← provider 限流（rate.windows）三态判定
              ⑤ provider 执行        ← mock / ollama / codex / claude
              ⑥ progress.node_exit   ← 执行后打卡
              ⑦ bus.node_done|node_error

V2 资源门（任务 2 挂钩点，ResourceManager 判定，executor 只管消费三态）：
    ACQUIRED → 正常执行
    BLOCKED  → 本次拒绝（队列满/状态非法）→ 节点失败（走 QA 回退三件套）
    RESTING  → 该 provider 进入强制休息 → 节点挂起（blocked=True），
               orchestrator 的 tick 等待循环在精确恢复时刻整体重跑该行

Phase 1 provider 策略不变：
    mock     → 确定性本地桩（零外部依赖，全链路可离线跑通；task 含 "sim_fail" 时失败）
Phase 2c 真实通道：
    registry 里能匹配到 openai-compat/ollama 条目 → LLMClient 真调 chat/completions，
    usage 回写资源账本（rm.settle 真实扣减）；429/503 → rm.force_rest 强按 RESTING；
    匹配不到 → 降级 mock 并打 shell_log 事件明示（不静默换道）。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Optional

from ..persistence.progress import ProgressLog
from ..schema.validator import LoadedWorkflow
from .events import EventBus
from .provider_registry import LLMClient, LLMResult, Provider, ProviderError, provider_for_agent
from .resource_manager import (RESOURCE_ACQUIRED, RESOURCE_BLOCKED,
                               RESOURCE_RESTING, TokenEstimator)


@dataclass
class NodeOutcome:
    """行调度器（orchestrator）消费的单节点结果。"""
    level: int
    node_id: str
    ok: bool
    outputs: dict[str, str] = field(default_factory=dict)   # 产物名 -> 文本（QA 输入来源）
    error: Optional[str] = None
    blocked: bool = False          # V2：RESTING 挂起（≠ 失败）：orchestrator tick 等待后重跑
    resume_at: float = 0.0        # V2：挂起节点的精确恢复时刻（tick/pump 目标）


class UpstreamIndex:
    """上游结果 + 产物字节索引（TokenEstimator 的数据源；executor 与 orchestrator 共用）。"""

    def __init__(self, outcomes: dict[str, "NodeOutcome"]):
        self.outcomes = outcomes
        self.upstream_bytes: dict[str, int] = {
            nid: sum(len(v) for v in o.outputs.values())
            for nid, o in outcomes.items() if o.ok
        }

    def estimate(self, node: dict[str, Any]) -> int:
        return TokenEstimator.estimate(node, self.upstream_bytes)


class NodeExecutor:
    def __init__(self, node: dict[str, Any], agent: dict[str, Any], wf: LoadedWorkflow,
                 bus: EventBus, progress: ProgressLog,
                 resource_manager: Optional[Any] = None,
                 git_policy: Optional[Any] = None,
                 llm_registry: Optional[list[Provider]] = None):
        self.node = node
        self.agent = agent
        self.wf = wf
        self.bus = bus
        self.progress = progress
        self.rm = resource_manager
        self.git_policy = git_policy
        self.llm_registry = llm_registry            # Phase 2c：全局模型配置中心（providers.json 快照）
        self.level: int = 0                      # orchestrator 在调度时填
        self.node_id = node["id"]
        self.provider = (agent or {}).get("provider", "mock")
        self.kind = node.get("kind", "agent")
        self.max_turns = int((agent or {}).get("maxTurns", 12))

    # ------------------------------------------------------------------ 入口
    async def run(self, upstream: Optional[UpstreamIndex] = None) -> NodeOutcome:
        task = self.node.get("task", "")
        upstream = upstream or UpstreamIndex({})

        # ① ★ 执行前打卡（Strict Constraint #2）：真正动手之前，先写 progress.md
        self.progress.node_enter(self.level, self.node_id, self.provider,
                                 next_step=f"执行任务: {task[:40]}…")

        # ② 事件：节点进入运行态（前端 AgentNode 转蓝色/转圈）
        await self.bus.node_started(self.level, self.node_id, self.provider)
        # ②b ★ V2：负载量事件（工位案卷高度 / 资源调度权重，前端数据源）
        estimate = upstream.estimate(self.node)
        await self.bus.publish("load_change", node_id=self.node_id,
                               estimate_tok=estimate,
                               source="declared" if (self.node.get("tokenBudget") or {}).get("estimate")
                                      is not None else "estimated")

        # ③ 解析上游输入 "nodeId.outputName" → 产物文本。
        #    缺任何一个 = 数据流断裂：agent 节点直接失败；qa/review 节点拦截（这是红框的常规来源）。
        missing: list[str] = []
        resolved: dict[str, str] = {}
        for ref in self.node.get("inputs", []):
            up_id, _, out_name = ref.partition(".")
            up = upstream.outcomes.get(up_id)
            if up is None or up.error or out_name not in up.outputs:
                missing.append(ref)
            else:
                resolved[ref] = up.outputs[out_name]

        # ④ ★ V2 资源门：LLM/工具执行前扣配额（RESTING = 强制休息，挂起等 tick 唤醒）
        verdict, resume_at = await self._acquire_resource()
        if verdict == RESOURCE_RESTING:
            outcome = NodeOutcome(self.level, self.node_id, ok=False, blocked=True,
                                  resume_at=resume_at,
                                  error=f"provider 强制休息中，{resume_at:.1f}s 后恢复（tick 唤醒）")
            await self.bus.publish("rate_wait", node_id=self.node_id, agent=self.node.get("agent"),
                                    resume_at=resume_at, estimate_tok=estimate)
        elif verdict == RESOURCE_BLOCKED:
            outcome = NodeOutcome(self.level, self.node_id, ok=False,
                                  error="资源门拒绝：SQUAD_DEPLOYED 期间不接受新分配")
        else:
            # ★ Phase 3a：pr_gate 节点 = 对上游 Dev 分支开 PR + 跑合并门禁（引擎权威层）
            if self.kind == "pr_gate":
                outcome = await self._run_pr_gate(resolved, upstream)
            elif missing:
                reason = (f"上游输入缺失: {missing}"
                          f"（{'QA 拦截' if self.kind in ('qa', 'review') else '数据流断裂'}）")
                outcome = NodeOutcome(self.level, self.node_id, ok=False, error=reason)
            else:
                outcome = await self._execute_provider(resolved, estimate)
                # ★ Phase 3a 分支隔离：Dev 节点成功后只在 <branch> 提交（缺省=节点 id），主干引擎直改被禁
                if outcome.ok and self.git_policy is not None:
                    outcome.outputs["branch"] = await self.git_policy.dev_submit(self.node, outcome.outputs)

        # ⑤ ★ 执行后打卡（挂起节点也打卡：状态可审计）
        self.progress.node_exit(self.level, self.node_id, self.provider,
                                completed=f"节点{'通过' if outcome.ok else ('挂起' if outcome.blocked else '失败')}:"
                                          f" {(outcome.error or task)[:40]}")

        # ⑥ 事件：终态（红框由 orchestrator 在 gate 判定后统一触发，这里只报节点级结果）
        if outcome.ok:
            await self.bus.node_finished(self.level, self.node_id, ok=True,
                                         output="; ".join(outcome.outputs.values())[:200])
        else:
            await self.bus.node_finished(self.level, self.node_id, ok=False,
                                         output=outcome.error or "")
        return outcome

    # -------------------------------------------------------- Phase 3a PR 门禁
    async def _run_pr_gate(self, resolved: dict[str, str],
                           upstream: UpstreamIndex) -> NodeOutcome:
        """pr_gate 节点：收集 inputs 引用的上游 Dev 节点 → 开 PR → 冲突+门禁脚本。
        全绿 → 节点 ok（产物带 merge sha 证据）；任一红灯 → 节点失败（orchestrator 红框三件套接手）。"""
        if self.git_policy is None:
            return NodeOutcome(self.level, self.node_id, ok=False,
                               error="pr_gate 节点需要 workflow.git_policy 配置（backend=simulated 起步）")
        # 上游 Dev 节点 = inputs 里引用的全部 node_id（它们的 branch 已被 dev_submit 记录）
        source_ids = []
        for ref in self.node.get("inputs", []):
            up_id = ref.partition(".")[0]
            if up_id in upstream.outcomes and up_id not in source_ids:
                source_ids.append(up_id)
        if not source_ids:
            return NodeOutcome(self.level, self.node_id, ok=False,
                               error="pr_gate 缺 inputs：至少要引用一个 Dev 节点分支")
        pr, checks = await self.git_policy.open_pr(self.node, source_ids)
        await self.bus.publish("pr_checks", node_id=self.node_id, pr=pr.pr_id,
                               state=pr.state, checks=checks)
        if pr.state == "merged":
            return NodeOutcome(self.level, self.node_id, ok=True,
                               outputs={"merge_sha": pr.merge_sha or "",
                                        "pr": pr.pr_id,
                                        "checks": "; ".join(f"{c['name']}:{'✓' if c['passed'] else '✗'}"
                                                           for c in checks)})
        # 红灯：明细进 error（红框高亮时前端/控制台可见原因）
        reds = "; ".join(f"[{c['name']}] {c['detail']}" for c in checks if not c["passed"])
        return NodeOutcome(self.level, self.node_id, ok=False,
                          error=f"PR {pr.pr_id} 被门禁拦截: {reds}")

    # -------------------------------------------------------- V2 资源门
    async def _acquire_resource(self) -> tuple[str, float]:
        """LLM 调用前扣 1 个"请求"配额（对齐 rate.windows 的 limit=次数/窗口，如 160次/60s）。
        tokenBudget.estimate 是工位负载/调度权重（前端案卷高度用），**不**进速率门——
        速率门数的是请求次数，负载量只是调度参考。"""
        if self.rm is None:
            return RESOURCE_ACQUIRED, 0.0
        return await self.rm.acquire(self.node.get("agent", ""), tokens=1)

    # ------------------------------------------------------------- provider 分派
    async def _execute_provider(self, resolved_inputs: dict[str, str], estimate: int) -> NodeOutcome:
        task = self.node.get("task", "")
        # task 含 "sim_fail" = 确定性失败钩子（演示 QA 拦截 + 红框 + 回退，不需要真 LLM）
        force_fail = "sim_fail" in task

        if self.provider == "mock":
            await self._run_mock(resolved_inputs, fail=force_fail, estimate=estimate)
            return self._pending
        # ★ Phase 2c：真实 LLM 通道（provider_registry 匹配注册表条目）
        entry = provider_for_agent(self.agent, self.llm_registry or [])
        if entry is None:
            # 注册表里没匹配 → 明示降级（不静默换道）
            await self.bus.publish("shell_log", node_id=self.node_id,
                                   cmd=f"$ provider:{self.provider} (providers.json 无匹配条目, 降级 mock; 预估 {estimate} tok)")
            await self._run_mock(resolved_inputs, fail=force_fail, estimate=estimate)
            return self._pending
        return await self._run_llm(entry, resolved_inputs, estimate)

    # -------------------------------------------------------- Phase 2c 真实 LLM 通道
    async def _run_llm(self, entry: Provider, resolved_inputs: dict[str, str], estimate: int) -> NodeOutcome:
        """真实 chat/completions：usage 结算进账本；429/503 → force_rest 强按 RESTING。
        sim_fail 钩子保留（真实模型上演示 QA 三件套仍可用）。"""
        agent_id = self.node.get("agent", self.node_id)
        task = self.node.get("task", "")
        system = (self.agent.get("systemPrompt")
                  or f"你是虚拟公司员工（{self.agent.get('role', self.provider)}），"
                     f"边界：{self.agent.get('boundaries')}。完成任务并输出产物。")
        user_parts = [f"任务：{task}"]
        if resolved_inputs:
            user_parts.append("上游产物（只读引用）：\n" +
                              "\n\n".join(f"--- {k} ---\n{v[:4000]}" for k, v in resolved_inputs.items()))
        model = self.agent.get("model") or None          # agent 级 model 优先；缺省 = 注册表条目 defaultModel

        await self.bus.publish("thinking", node_id=self.node_id,
                               text=f"[llm:{entry.name}] 调 {entry.base_url} (model={model or entry.default_model}),"
                                    f" 预估 {estimate} tok")
        try:
            res: LLMResult = await entry.client(model).chat(system, "\n\n".join(user_parts), model=model)
        except ProviderError as e:
            # ★ 429/503 捕获（任务 3）：强按 RESTING（provider 级扩散），挂起等 tick 唤醒
            if e.status in (429, 503) and self.rm is not None:
                resume = self.rm._now() + (e.retry_after or 30.0)
                await self.bus.publish("rate_limit_hit", agent=agent_id, resume_at=resume,
                                       reason=f"llm_http_{e.status}", source="live_api",
                                       retry_after=e.retry_after)
                await self.rm.force_rest(agent_id, resume, reason=f"llm_http_{e.status}")
                self._pending = NodeOutcome(self.level, self.node_id, ok=False, blocked=True,
                                            resume_at=resume,
                                            error=f"模型端限流 HTTP {e.status}，{e.retry_after or 30:.0f}s 后恢复（tick 唤醒）")
            else:
                self._pending = NodeOutcome(self.level, self.node_id, ok=False,
                                            error=f"LLM 调用失败: {e}")
                await self.bus.publish("shell_log", node_id=self.node_id, cmd=f"LLM 错误: {e}")
            return self._pending

        # ★ 真实 usage 结算（任务 3"真实请求扣减"）：补扣进 provider 账本；补扣触限 = 强按 RESTING
        if self.rm is not None:
            allowed, resume_at = self.rm.settle(agent_id, res.usage.get("total_tokens", 0))
            await self.bus.publish("usage_settle", node_id=self.node_id, agent=agent_id,
                                   usage=res.usage, model=res.model, provider=entry.name,
                                   estimated=estimate)
            if not allowed:
                await self.rm.force_rest(agent_id, resume_at or self.rm._now() + 30.0, reason="usage_settle")
                self._pending = NodeOutcome(self.level, self.node_id, ok=False, blocked=True,
                                            resume_at=resume_at or 0.0,
                                            error=f"真实用量触限（{res.usage.get('total_tokens')} tok），强制休息")
                return self._pending

        # sim_fail 钩子在真实通道也生效（离线演示 QA 三件套不依赖假模型）
        if "sim_fail" in task:
            self._pending = NodeOutcome(self.level, self.node_id, ok=False,
                                        error=f"sim_fail 钩子（真实 LLM 已跑, usage={res.usage}）")
            return self._pending

        declared = [o.get("name", "report") for o in self.agent.get("outputs", [])] or ["report"]
        artifacts = {name: res.content for name in declared}
        self._pending = NodeOutcome(self.level, self.node_id, ok=True, outputs=artifacts)
        return self._pending

    async def _run_mock(self, resolved_inputs: dict[str, str], fail: bool = False, estimate: int = 0) -> None:
        """确定性 mock：模拟 ReAct 两拍（思考 + 工具调用 + 产物），带微小延迟让前端能看到并发。"""
        await self.bus.publish("thinking", node_id=self.node_id,
                               text=f"[mock:{self.provider}] 分析任务: {self.node.get('task', '')[:60]}"
                                    + (f" (预估 {estimate} tok)" if estimate else ""))
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
