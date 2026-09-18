/**
 * LiveLogPanel.tsx —— 实时日志时间线（agent-flow-visualization 式透明化面板）。
 * 事件着色：thinking=青、tool_call=黄、shell_log=终端绿、red_highlight/rollback=红。
 */
import { useEffect, useRef } from "react";
import { useWorkflowStore } from "../../store/workflowStore";
import type { BusEvent } from "../../lib/types";

const COLOR: Record<string, string> = {
  thinking: "#5eead4",
  tool_call: "#fde047",
  shell_log: "#4ade80",
  node_start: "#38bdf8",
  node_done: "#22c55e",
  node_error: "#f87171",
  red_highlight: "#ef4444",
  rollback: "#f97316",
  level_start: "#818cf8",
  run_start: "#a78bfa",
  run_finish: "#34d399",
};

export default function LiveLogPanel() {
  const events = useWorkflowStore((s) => s.logEvents);
  const runId = useWorkflowStore((s) => s.runId);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [events]);

  return (
    <div style={{
      width: 340, overflowY: "auto", padding: 10,
      background: "#0f172a", borderLeft: "1px solid #1e293b",
      fontFamily: "ui-monospace, Consolas, monospace", fontSize: 11,
      display: "flex", flexDirection: "column",
    }}>
      <div style={{ color: "#94a3b8", marginBottom: 8, fontWeight: 700 }}>
        LIVE LOG {runId ? `· run ${runId}` : "· 未连接"}（shell/思考/工具 全透明）
      </div>
      {events.length === 0 && <div style={{ color: "#475569" }}>等待 /ws/run 事件…</div>}
      {events.map((ev: BusEvent, i) => {
        const d = ev.data || {};
        const line = formatLine(ev);
        const age = events.length - 1 - i;                 // 越旧越淡（新事件全亮）
        return (
          <div key={i} style={{
            color: COLOR[ev.type] ?? "#cbd5e1",
            borderLeft: `2px solid ${COLOR[ev.type] ?? "#334155"}`,
            padding: "2px 6px", marginBottom: 3, whiteSpace: "pre-wrap",
            opacity: Math.max(0.35, 1 - age * 0.06),
          }}>
            {line}
          </div>
        );
      })}
      <div ref={endRef} />
    </div>
  );
}

function formatLine(ev: BusEvent): string {
  const d = ev.data || {};
  const ts = new Date(ev.ts * 1000).toISOString().slice(11, 19);
  switch (ev.type) {
    case "shell_log":
      return `${ts} $ ${(d.cmd as string) ?? ""}\n   ${(d.line as string) ?? ""}`;
    case "thinking":
      return `${ts} ◉ ${(d.node_id as string)} ${(d.text as string)}`;
    case "tool_call":
      return `${ts} ⚙ ${(d.node_id as string)} → ${(Array.isArray(d.tool) ? d.tool.join(",") : d.tool)}`;
    case "red_highlight":
      return `${ts} ⛔ L${d.level} ${d.node_id} 红框拦截: ${d.reason}`;
    case "rollback":
      return `${ts} ↩ 回退 L${d.from_level} → L${d.to_level}（第 ${d.attempt} 次）`;
    case "run_finish":
      return `${ts} ■ run ${ev.run_id} ${d.success ? "SUCCESS" : "FAILED"} (${d.seconds}s)`;
    default:
      return `${ts} · ${ev.type} ${JSON.stringify(d).slice(0, 100)}`;
  }
}
