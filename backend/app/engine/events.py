"""
事件总线：把引擎里发生的一切（节点开始/结束、思考、工具调用、Shell 日志、
QA 红框/回退）广播给所有订阅者。

架构对齐 gabriel-eidelman/agent-flow-visualization：
    引擎 → EventBus.publish(event) ──┬──> ws/stream.py (WebSocket 广播给前端)
                                      └──> 控制台 (stderr 镜像, 终端黑盒也可见)

设计要点：
- 每个 workflow 一个 EventBus 实例（run_id 维度），避免并发 run 互相串流；
- subscribe() 返回的回调必须是非阻塞的（WS 层用 asyncio.create_task 转投）；
- 事件统一带 ts/run_id，前端据此做时间线与红框定位。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

# 事件类型（前端 LiveLogPanel / AgentNode 状态机都认这套词汇）
EVENT_TYPES = (
    "run_start", "run_finish",
    "level_start", "level_done",
    "node_start", "node_done", "node_error",
    "thinking", "tool_call", "shell_log",
    "qa_pass", "qa_fail", "red_highlight", "rollback",
    "error",
)


@dataclass
class Event:
    ts: float
    run_id: str
    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"ts": self.ts, "run_id": self.run_id, "type": self.type, "data": self.data}


# 订阅回调：同步函数或 async 函数都支持
Subscriber = Callable[[Event], Awaitable[None] | None]


class EventBus:
    """进程内事件总线。publish 为异步方法，WS 侧逐条扇出。"""

    def __init__(self, run_id: Optional[str] = None):
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self._subscribers: list[Subscriber] = []

    def subscribe(self, fn: Subscriber) -> None:
        self._subscribers.append(fn)

    async def publish(self, type_: str, **data: Any) -> None:
        """发布事件。未知类型宽容放行（data 里带 type），但标准类型走 EVENT_TYPES。"""
        ev = Event(ts=time.time(), run_id=self.run_id, type=type_, data=data)
        for fn in self._subscribers:
            res = fn(ev)
            if hasattr(res, "__await__"):      # 异步订阅者
                await res
        # 控制台镜像：终端永远能看到黑盒内部（对齐"打破运行黑盒"目标）
        print(f"[bus|{ev.run_id}] {type_:<12} {json.dumps(data, ensure_ascii=False, default=str)[:400]}",
              flush=True)

    # ---- 常用语义化快捷方法（引擎侧写起来干净） ----
    async def node_started(self, level: int, node_id: str, agent: str) -> None:
        await self.publish("node_start", level=level, node_id=node_id, agent=agent)

    async def node_finished(self, level: int, node_id: str, ok: bool, output: str = "") -> None:
        await self.publish("node_done" if ok else "node_error",
                           level=level, node_id=node_id, output=output[:500])

    async def red_highlight(self, level: int, node_id: str, reason: str) -> None:
        """QA 拦截/节点失败 → 前端 AgentNode 外框标红 + 控制台。"""
        await self.publish("red_highlight", level=level, node_id=node_id, reason=reason)
