/**
 * runSocket.ts —— Task 3 消费端：/ws/run WebSocket 客户端。
 *
 * 连接后端 ws/stream.py：
 *   ws://host:8790/ws/run[?run_id=xxx]  →  事件 JSON 流（thinking/tool_call/shell_log/...）
 *   → store.onEvent(ev)  →  节点变色 + LiveLogPanel 时间线
 *
 * 终端日志（Codex 跑 npm run build 的输出）就是 shell_log 事件的一种：
 * 后端 executor 逐行读子进程 stdout 后 publish，前端这里零改动自动显示。
 */
import type { BusEvent } from "../lib/types";

export interface SocketHandlers {
  onEvent: (ev: BusEvent) => void;
  onStatus?: (s: "open" | "closed" | "error") => void;
}

export function connectRunSocket(
  url: string,
  handlers: SocketHandlers,
): () => void {
  let ws: WebSocket | null = null;
  let closed = false;

  const open = () => {
    if (closed) return;
    ws = new WebSocket(url);
    ws.onopen = () => handlers.onStatus?.("open");
    ws.onclose = () => {
      handlers.onStatus?.("closed");
      if (!closed) setTimeout(open, 2000);          // 断线重连（引擎重启不丢流）
    };
    ws.onerror = () => handlers.onStatus?.("error");
    ws.onmessage = (m) => {
      try {
        handlers.onEvent(JSON.parse(m.data) as BusEvent);
      } catch {
        /* 非 JSON 帧忽略（引擎握手/心跳） */
      }
    };
  };
  open();

  return () => {                                     // 卸载清理
    closed = true;
    ws?.close();
  };
}
