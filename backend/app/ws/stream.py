"""
WebSocket 日志总线雏形（Task 3）：引擎事件 → 前端实时流。

    引擎 EventBus ──publish──> Hub.broadcast() ──> 每个已连接 WS 客户端
                                                       (前端 LiveLogPanel 按 ts 渲染时间线,
                                                        red_highlight 事件 → AgentNode 外框标红)

后续终端日志（如 Codex 执行 npm run build 的输出）的接入方式：
    1. executor 里 provider 启动子进程（codex CLI / shell 命令）；
    2. 逐行读 stdout：`async for line in proc.stdout`；
    3. 每行 publish 一条 shell_log 事件：
         await bus.publish("shell_log", node_id=..., cmd=..., line=line, stream="stdout"|"stderr")
    4. Hub 自动扇出 —— 前端零改动，只需在 LiveLogPanel 里给 shell_log 加个终端样式。
   （即：进程输出不是"另一条通道"，就是事件总线里的一类事件，黑盒从此不存在。）

连接管理用 asyncio.Queue 做背压解耦：
    Hub 持有一个 asyncio.Queue 当写缓冲，_pump 协程从 Queue 取事件逐个发给各客户端；
    慢客户端的 Queue 积压不影响快客户端（每客户端独立 Queue + maxsize 丢旧保护）。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from ..engine.events import Event, EventBus, Subscriber


class Hub:
    """一个进程内的广播中枢（多 run 并发场景按 run_id 过滤，Phase 2 再分片）。"""

    def __init__(self, bus: EventBus):
        self.bus = bus
        self._clients: list[tuple[WebSocket, asyncio.Queue]] = []

    def attach(self) -> None:
        """进程启动时调一次：把 Hub 接到事件总线。"""
        self.bus.subscribe(self._make_subscriber())

    def link(self, run_bus: EventBus) -> None:
        """把某个 run 的 EventBus 接进 Hub：run 内事件同样扇出给所有客户端。
        客户端在 WS 层用 ?run_id= 过滤（_pump 里处理）。"""
        run_bus.subscribe(self._make_subscriber())

    def _make_subscriber(self) -> Subscriber:
        """EventBus 侧：把事件投进每个客户端的 Queue（背压解耦点）。"""
        def sub(event: Event) -> None:
            for ws, q in self._clients:
                if q.full():
                    try:
                        q.get_nowait()          # 慢客户端：丢最旧事件（终端日志宁可保新弃旧）
                    except asyncio.QueueEmpty:
                        pass
                q.put_nowait(event)
        return sub

    # ------------------------------------------------------------ 连接生命周期
    async def connect(self, ws: WebSocket, run_id: str | None = None) -> None:
        await ws.accept()
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=2048)
        entry = (ws, q)
        self._clients.append(entry)
        pump = asyncio.create_task(self._pump(ws, q, run_id))
        try:
            while True:
                # 入站消息仅支持 {"type":"filter_run","run_id":...}（Phase 2 需求预留）
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            pump.cancel()
            if entry in self._clients:
                self._clients.remove(entry)

    # ------------------------------------------------------------ 出站泵
    @staticmethod
    async def _pump(ws: WebSocket, q: asyncio.Queue, run_id: str | None) -> None:
        """每个连接一个 pump 协程：Queue → WebSocket。send 失败即断开摘除。"""
        try:
            while True:
                ev: Event = await q.get()
                if run_id and ev.run_id != run_id:
                    continue                     # 客户端指定了 run 过滤（多 run 并发时）
                await ws.send_text(json.dumps(ev.to_dict(), ensure_ascii=False, default=str))
        except (WebSocketDisconnect, RuntimeError, ConnectionError):
            pass


def stream_endpoint(hub: Hub):
    """FastAPI 路由工厂：返回 /ws/run 端点（带 run_id 查询参数过滤）。"""
    async def _ws_run(ws: WebSocket, run_id: str | None = None) -> None:
        await hub.connect(ws, run_id)
    return _ws_run
