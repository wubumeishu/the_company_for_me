"""
resource_manager.py —— Phase 2 核心："API 冷却即强制休息" 资源调度器。

三个构件（对齐 ARCHITECTURE_V2 §1.2 / §1.3）：

  RateLedger(provider)
    多窗口滑动计数：windows=[{limit:160, periodSec:60}, {limit:1500, periodSec:18000}, ...]
    consume(n, now)   → (allowed, resume_at)：任一窗口触底 = 拒绝，resume_at = 最早恢复点
    next_reset(now)   → 所有窗口的下次重置时刻（Tick 唤醒判定用）

  ResourceManager(wf, bus, clock)
    Agent 状态机（每 agent 一个）：
        ACTIVE ──consume 触限──> RESTING(resume_at, reason)
        RESTING ──tick: now>=resume_at──> ACTIVE（自动唤醒，发 rest_over）
        ACTIVE ──squad 抽调──> SQUAD_DEPLOYED ──解散──> ACTIVE
    acquire(agent_id, tokens, now) → 执行前配额门：扣减成功放行；触限则置 RESTING
                                      并精确算出 resume_at（= 相关窗口最早重置点 + cooldownSec）
    wake_due(now)   → TickDriver 每拍调用：所有到期 RESTING 员工回 ACTIVE
    snapshot(now)   → {agent_id: {state, resume_at, windows_remain}}（WS tick 事件 / 站会大盘源）

  TickDriver(interval, clock, sleep)
    固定心跳（默认 0.5s，可 1s）。构造参数注入 clock/sleep → 单测可"冻结时间"精确断言
    冷却恢复时刻，不必真等 60s。

设计铁律（防御性编程，同 Phase 1 心态）：
  - 所有时间都走 self._now()（可注入），禁止函数体内直读 time.time()——恢复时间可测可回放；
  - 无 provider.rate 配置的 agent = 不限量（acquire 恒放行），旧 workflow 行为零变化（向后兼容）；
  - 事件全进 EventBus：rate_window / rate_limit_hit / rest_over / state_change（WS 总线零改动）。
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol

from .events import EventBus

# --------------------------------------------------------------------------- 时钟
Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


def wall_clock() -> float:
    return time.time()


# --------------------------------------------------------------------------- TokenEstimator
class TokenEstimator:
    """tokenBudget 缺省时的启发式补全（ARCHITECTURE_V2 §1.1：后端必须开始处理预估量数据）。

    estimate ≈ task 提示词字数(≈0.3 token/字, 中英混合保守)
             + 上游输入产物的 字节/4（LLM 分词近似）
             + outputHint（声明的期望输出量，缺省 256）
    """

    @staticmethod
    def estimate(node: dict[str, Any], upstream_bytes: dict[str, int]) -> int:
        declared = (node.get("tokenBudget") or {}).get("estimate")
        if declared is not None:
            return int(declared)                     # 作者写死优先
        text = node.get("task", "")
        base = max(64, int(len(text) * 0.3))
        up = sum(upstream_bytes.values())
        out_hint = (node.get("tokenBudget") or {}).get("outputHint")
        return base + up // 4 + (out_hint or 256)


# --------------------------------------------------------------------------- RateLedger
@dataclass
class WindowSpec:
    limit: int
    period_sec: int
    cooldown_sec: int = 0


class RateLedger:
    """单 provider 的多窗口滑动配额账本（分钟级 + 小时级…）。

    每个窗口独立记 deque[(ts, amount)]，consume 前把 < now-period 的旧戳清掉再判余量。
    Phase 2c 升级：每笔入账带 amount（token 量）。调用数语义 = 每笔 amount=1（Phase 2a
    行为不变）；真实 LLM 结算 = 补扣真实 token 量（settle 路径）。
    无 windows（provider 没配 rate）= 恒放行，resume_at=None。
    """

    def __init__(self, windows: list[WindowSpec], cooldown_default: int = 0):
        self.windows = windows
        self._hits: dict[int, deque[tuple[float, int]]] = {i: deque() for i in range(len(windows))}
        self.cooldown_default = cooldown_default

    def _used(self, i: int, now: float) -> int:
        """窗口内已用配额（各笔 amount 之和；amount=1 时 = 调用数，2a 语义零变化）。"""
        self._prune(i, now)
        return sum(amount for _, amount in self._hits[i])

    def _prune(self, i: int, now: float) -> None:
        q = self._hits[i]
        spec = self.windows[i]
        while q and now - q[0][0] >= spec.period_sec:
            q.popleft()

    def remaining(self, now: float) -> list[dict[str, Any]]:
        """各窗口余量快照（rate_window 事件 / UI 环形倒计时用）。"""
        out = []
        for i, spec in enumerate(self.windows):
            used = self._used(i, now)
            left = spec.limit - used
            out.append({"limit": spec.limit, "periodSec": spec.period_sec,
                        "used": used, "remaining": max(left, 0),
                        "resetAt": self.next_reset(i, now)})
        return out

    def next_reset(self, window_idx: int, now: float) -> float:
        """窗口最早可用时刻（量感知）：还放得下 1 单位=now；否则=最旧一笔戳 + 周期
        （即该笔账龄满、窗口量释放的最早时刻）。remaining() 快照 / UI 环形倒计时用。"""
        self._prune(window_idx, now)
        spec = self.windows[window_idx]
        q = self._hits[window_idx]
        if not q:
            return now
        if self._used(window_idx, now) + 1 <= spec.limit:
            return now
        return q[0][0] + spec.period_sec

    def consume(self, tokens: int = 1, now: float = 0.0,
                now_fn: Optional[Clock] = None) -> tuple[bool, Optional[float]]:
        """扣减 n 个配额。返回 (allowed, resume_at)。

        任一窗口扣满 → allowed=False，resume_at = 所有"本次参与触限窗口"的
        next_reset + 该窗口 cooldown 的最大值（最保守恢复点）。
        """
        if now_fn:
            now = now_fn()
        if not self.windows:
            return True, None

        projected: list[WindowSpec] = []
        for i, spec in enumerate(self.windows):
            used = self._used(i, now)
            if used + tokens > spec.limit:
                projected.append(spec)
        if not projected:
            for i in range(len(self.windows)):
                self._hits[i].append((now, tokens))
            return True, None

        # 触限：最保守恢复点（量感知）= 各触限窗口中"最早一笔账龄满后余量可容纳本次 n 单位"的时刻
        # + 该窗口 cooldown；多窗口取最大值（最保守恢复点）。amount=1 时退化为 2a 的"最旧戳+周期"。
        resume = now
        for i, spec in enumerate(self.windows):
            if spec not in projected:
                continue
            q = self._hits[i]
            cooldown = spec.cooldown_sec or self.cooldown_default
            placed = False
            for t in sorted({ts + spec.period_sec for ts, _ in q}):
                used_at_t = sum(amount for ts, amount in q if ts > t - spec.period_sec)
                if used_at_t + tokens <= spec.limit:
                    resume = max(resume, t + cooldown)
                    placed = True
                    break
            if not placed:
                resume = max(resume, now + spec.period_sec + cooldown)
        return False, resume


# --------------------------------------------------------------------------- 状态
STATE_ACTIVE = "ACTIVE"
STATE_RESTING = "RESTING"
STATE_SQUAD = "SQUAD_DEPLOYED"

# acquire() 三态判定（executor/orchestrator 消费）
RESOURCE_ACQUIRED = "acquired"      # 配额扣减成功，放行执行
RESOURCE_BLOCKED = "blocked"       # SQUAD_DEPLOYED 等非法态，本次拒绝（节点失败）
RESOURCE_RESTING = "resting"       # provider 强制休息：挂起，resume_at 时刻 tick 唤醒


@dataclass
class AgentState:
    agent_id: str
    state: str = STATE_ACTIVE
    resume_at: Optional[float] = None          # RESTING 精确恢复时刻
    reason: Optional[str] = None              # 触发原因（rate_limit/stamina/squad）

    @property
    def is_resting(self) -> bool:
        return self.state == STATE_RESTING


# --------------------------------------------------------------------------- ResourceManager
class ResourceManager:
    """全公司资源账本 + 状态拦截 + Tick 唤醒（任务 2 主体）。"""

    # Agent.provider 词汇 → Provider.type 词汇（agent 侧写 claude，provider 注册表写 anthropic）
    PROVIDER_ALIAS = {
        "claude": ("claude", "anthropic"),
        "codex": ("codex", "codex-cli"),
        "ollama": ("ollama",),
        "mock": ("mock",),
        "builtin": ("builtin",),
    }

    def __init__(self, wf: "LoadedWorkflow", bus: EventBus, clock: Optional[Clock] = None):
        from ..schema.validator import LoadedWorkflow  # noqa 仅类型用（避免与 orchestrator 循环依赖）
        self.wf = wf
        self.bus = bus
        self._now: Clock = clock or wall_clock
        self.ledgers: dict[str, RateLedger] = {}
        self.states: dict[str, AgentState] = {}

        # provider → 该 provider 名下所有 agent（限流按 provider 生效，agent 共享窗口）
        by_provider: dict[str, list[str]] = {}
        for aid, agent in wf.agents.items():
            by_provider.setdefault(agent.get("provider", "mock"), []).append(aid)

        for prov in by_provider:
            specs = self._provider_specs(prov)
            cooldown = int(self._provider_cooldown(prov))
            self.ledgers[prov] = RateLedger(specs, cooldown)

        for aid in wf.agents:
            self.states[aid] = AgentState(aid)

    # ---- 配置解析（provider 没配 rate = 不限量，向后兼容）----
    def _find_provider(self, prov: str) -> Optional[dict[str, Any]]:
        # agent 侧 provider 词汇经别名表匹配 providers[] 注册表（id 或 type）
        candidates = self.PROVIDER_ALIAS.get(prov, (prov,))
        for p in self.wf.raw.get("providers", []):
            if p.get("id") in candidates or p.get("type") in candidates:
                return p
        return None

    def _provider_specs(self, prov: str) -> list[WindowSpec]:
        p = self._find_provider(prov)
        rate = (p or {}).get("rate") or {}
        return [WindowSpec(int(w["limit"]), int(w["periodSec"]),
                           int(w.get("cooldownSec", 0)))
                for w in rate.get("windows", [])]

    def _provider_cooldown(self, prov: str) -> int:
        p = self._find_provider(prov)
        return ((p or {}).get("rate") or {}).get("cooldownSec", 0)

    # ---- 核心：执行前配额门（executor 每次 LLM/工具调用前必过）----
    async def acquire(self, agent_id: str, tokens: int = 1,
                      wait: bool = False, timeout: float = 1800.0,
                      on_wait: Optional[Any] = None) -> tuple[str, float]:
        """返回 (verdict, resume_at)：
          ACQUIRED  配额扣减成功（本次调用放行，tokens 已入账）
          RESTING   provider 强制休息：本次挂起；wait=True 时原地等到 resume_at 后自动重试
                    （orchestrator 行挂起用；UI 倒计时数据 = resume_at - now）
          BLOCKED   SQUAD_DEPLOYED 等非法态，本次拒绝
        """
        st = self.states.get(agent_id)
        if st is None:
            return RESOURCE_ACQUIRED, 0.0          # 未注册 agent：不拦（旧配置向后兼容）

        while True:
            now = self._now()
            if st.state == STATE_SQUAD:
                return RESOURCE_BLOCKED, 0.0      # 抽调期间冻结派活（敏捷小队隔离语义）

            if st.state == STATE_RESTING:
                if st.resume_at is not None and now < st.resume_at:
                    if not wait:
                        return RESOURCE_RESTING, st.resume_at
                    await self._wait_until(st.resume_at, timeout, on_wait)
                    continue                      # 到期：继续走扣减判定
                # 到期未唤醒（tick 没跑/时钟竞态）：直接补判
                st.state = STATE_ACTIVE

            prov = self.wf.agents[agent_id].get("provider", "mock")
            was_active = (st.state == STATE_ACTIVE)
            allowed, resume_at = self._do_consume(agent_id, tokens, now)
            if allowed:
                return RESOURCE_ACQUIRED, 0.0

            # 触限事件只在 ACTIVE→RESTING 转变时发一次（同窗口多节点不再刷屏）
            if was_active:
                await self.on_limit_hit(agent_id, resume_at or now)
            if not wait:
                return RESOURCE_RESTING, resume_at if resume_at is not None else now
            await self._wait_until(resume_at or now, timeout, on_wait)
            # 回到循环：重新按 provider 全体判定（tick 可能已在等待中唤醒）

    async def _wait_until(self, target: float, timeout: float, on_wait: Optional[Any]) -> None:
        """挂起等待恢复时刻（测试注入 on_wait 可跳过真睡；timeout 超限抛出）。"""
        now = self._now()
        delay = max(0.0, target - now)
        if delay <= 0:
            return
        if delay > timeout:
            raise TimeoutError(f"RESTING 恢复时刻 {target:.0f}s 超出等待上限 {timeout}s")
        if on_wait is not None:
            await on_wait(delay)
        else:
            import asyncio
            await asyncio.sleep(delay)

    def _do_consume(self, agent_id: str, tokens: int, now: float) -> tuple[bool, Optional[float]]:
        """纯判定+入账：扣配额；触限则把该 provider 名下非 SQUAD 的 agent 全置 RESTING。
        事件发布走 on_limit_hit（acquire 路径统一发，判定路径不阻塞）。"""
        prov = self.wf.agents[agent_id].get("provider", "mock")
        ledger = self.ledgers.get(prov)
        if ledger is None or not ledger.windows:
            return True, None          # 该 provider 未配限流 → 恒放行（向后兼容）

        allowed, resume_at = ledger.consume(tokens, now_fn=lambda: now)
        if allowed:
            return True, None

        # 触限 → 该 provider 名下全部 agent 强制 RESTING（API 冷却是 provider 级事实）
        for aid, st in self.states.items():
            if self.wf.agents[aid].get("provider") == prov and st.state != STATE_SQUAD:
                st.state = STATE_RESTING
                st.resume_at = resume_at
                st.reason = "rate_limit"
        return False, resume_at

    async def on_limit_hit(self, agent_id: str, resume_at: float, reason: str = "rate_limit") -> None:
        """触限事件发布点（acquire 返回 False 后由调用方发出，不阻塞判定路径）。
        窗口余量快照随事件带出 → 前端"咖啡厅冷却"态的环形倒计时数据源。"""
        prov = self.wf.agents[agent_id].get("provider", "mock")
        led = self.ledgers.get(prov)
        windows = led.remaining(resume_at) if led else []
        await self.bus.publish("rate_limit_hit", agent=agent_id, resume_at=resume_at,
                               windows=windows, reason=reason)
        await self.bus.publish("state_change", agent=agent_id, state=STATE_RESTING,
                               resume_at=resume_at, reason=reason)

    # ---- 真实 LLM 结算（Phase 2c：usage 账本 + 429/503 强按 RESTING）----
    def settle(self, agent_id: str, actual_tokens: int,
               now: Optional[float] = None) -> tuple[bool, Optional[float]]:
        """真实 usage 入账（任务 3"真实请求扣减"）：acquire 时按请求扣了 1 单位，
        这里补扣 (实际 - 1) 个 token 单位进 provider 账本。返回 (allowed, resume_at)：
        补扣触限 → 调用方把名下全员强按 RESTING（与触限路径同语义，账本不因真实用量失真）。"""
        prov = self.wf.agents[agent_id].get("provider", "mock")
        ledger = self.ledgers.get(prov)
        if ledger is None or not ledger.windows:
            return True, None                       # 未配限流 = 恒放行（向后兼容铁律）
        extra = max(0, int(actual_tokens) - 1)
        if extra == 0:
            return True, None
        return ledger.consume(extra, now_fn=lambda: now if now is not None else self._now())

    async def force_rest(self, agent_id: str, resume_at: float, reason: str = "rate_limit") -> None:
        """429/503 捕获入口（executor 消费点）：立即 RESTING + 事件。
        走 provider 级扩散（同触限语义：API 冷却是 provider 事实，名下非 SQUAD 全员进咖啡厅）。"""
        prov = self.wf.agents[agent_id].get("provider", "mock")
        for aid, st in self.states.items():
            if self.wf.agents[aid].get("provider") == prov and st.state != STATE_SQUAD:
                st.state = STATE_RESTING
                st.resume_at = resume_at
                st.reason = reason
        await self.on_limit_hit(agent_id, resume_at, reason=reason)

    # ---- Tick 心跳：到期唤醒（任务 2 核心，可注入 clock 单测）----
    async def tick(self, now: Optional[float] = None) -> list[str]:
        """检查全部 RESTING 员工是否到期；到期 → ACTIVE + rest_over 事件。返回本拍唤醒名单。"""
        now = now if now is not None else self._now()
        woke: list[str] = []
        for st in self.states.values():
            if st.state != STATE_RESTING:
                continue
            if st.resume_at is not None and now >= st.resume_at:
                st.state = STATE_ACTIVE
                st.resume_at = None
                st.reason = None
                woke.append(st.agent_id)
                await self.bus.publish("rest_over", agent=st.agent_id, resumed_at=now)
                await self.bus.publish("state_change", agent=st.agent_id, state=STATE_ACTIVE)
        return woke

    # ---- 敏捷小队抽调/归建（任务 3 的状态接入点）----
    def deploy_squad(self, agent_ids: list[str]) -> None:
        for aid in agent_ids:
            st = self.states.get(aid)
            if st is not None and st.state == STATE_ACTIVE:
                st.state = STATE_SQUAD
                st.reason = "squad"

    def dissolve_squad(self, agent_ids: list[str]) -> None:
        for aid in agent_ids:
            st = self.states.get(aid)
            if st is not None and st.state == STATE_SQUAD:
                st.state = STATE_ACTIVE
                st.reason = None

    # ---- 大盘/WS 快照 ----
    def snapshot(self, now: Optional[float] = None) -> dict[str, dict[str, Any]]:
        now = now if now is not None else self._now()
        out: dict[str, dict[str, Any]] = {}
        for aid, st in self.states.items():
            prov = self.wf.agents[aid].get("provider", "mock")
            led = self.ledgers.get(prov)
            out[aid] = {
                "state": st.state,
                "resume_at": st.resume_at,
                "remaining_sec": (st.resume_at - now) if (st.state == STATE_RESTING and st.resume_at) else None,
                "windows": led.remaining(now) if led and led.windows else [],
                "department": self.wf.agents[aid].get("department"),
            }
        return out

    async def publish_tick(self, now: Optional[float] = None) -> None:
        """每拍状态全量（TickDriver 调用；低频聚合防 WS 刷屏，ARCHITECTURE_V2 开放问题 #1 的解法：
        大盘 1s 聚合一次，事件数据量 = agent 数 × O(1)）。"""
        now = now if now is not None else self._now()
        await self.bus.publish("tick", states=self.snapshot(now))


# --------------------------------------------------------------------------- TickDriver
class TickDriver:
    """固定心跳驱动（对齐 V2 §1.3：每 tick 检 RESTING 唤醒 + 发状态快照）。

    注入 clock/sleep → 单测可"快进"任意时长不真等待。
    生命周期：orchestrator.run() 内 start()，run 结束 stop()。
    """

    def __init__(self, rm: ResourceManager, interval: float = 0.5,
                 clock: Optional[Clock] = None,
                 sleep: Optional[Sleep] = None,
                 emit_every: int = 2):
        self.rm = rm
        self.interval = interval
        self._sleep_fn: Sleep = sleep                     # 注入 sleep 时全走它（单测可快进）
        self._real_sleep: bool = sleep is None            # None = 生产模式，真 asyncio.sleep
        self._clock = clock
        self._emit_every = emit_every        # 每 N 拍发一次 tick 快照（默认 2 拍 = 1s @0.5s）
        self._task: Optional[Awaitable[Any]] = None
        self._stopping = False
        self._tick_count = 0

    def _now(self) -> float:
        return self._clock() if self._clock else time.time()

    async def _wait_interval(self) -> None:
        if self._real_sleep:
            import asyncio
            await asyncio.sleep(self.interval)
        else:
            await self._sleep_fn(self.interval)           # 测试快进通道

    async def start(self) -> None:
        """启动心跳 loop（生产路径：orchestrator.run() 期间随 run 起停）。
        loop 本身在外部 await（run 协程里 create_task），这里只负责"跑一拍到 stopping"。"""
        import asyncio
        self._stopping = False
        loop_task = asyncio.create_task(self._run_loop())
        # 把 loop 任务暴露给 stop()；等待它自然结束或外部取消
        self._loop_task = loop_task
        await asyncio.sleep(0)          # 让出首拍调度权

    async def _run_loop(self) -> None:
        while not self._stopping:
            await self.tick_once()
            await self._wait_interval()

    async def tick_once(self) -> list[str]:
        """单拍：唤醒到期员工 + 按节奏发快照。手动/测试可直接调，不必起 loop。
        第 1 拍无条件发 baseline 快照（大盘初始态），之后每 emit_every 拍或有人唤醒才发。"""
        woke = await self.rm.tick()
        self._tick_count += 1
        if self._tick_count == 1 or self._tick_count % self._emit_every == 0 or woke:
            await self.rm.publish_tick()
        return woke

    async def stop(self) -> None:
        self._stopping = True
        import asyncio
        task = getattr(self, "_loop_task", None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
