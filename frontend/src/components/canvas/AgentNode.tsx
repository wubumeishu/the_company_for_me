/**
 * AgentNode.tsx —— 自定义节点：角色/工具徽章 + Provider 徽章 + 失败红框。
 *
 * 样式约定（Strict Constraint #3 的可视化落地）：
 *   idle    → 灰边
 *   running → 蓝边 + 呼吸光
 *   ok      → 绿边
 *   error   → ★ 外边框 2px 红 + 红光晕（流转图红框高亮，一眼看到拦截点）
 * Provider 徽章（Ollama/Codex/Claude/Mock）用不同底色区分本地 vs 云端。
 */
import { memo } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { NodeStatus } from "../../lib/types";
const PROVIDER_STYLE: Record<string, string> = {
  ollama: "background:#1b5e20;color:#a5d6a7;",   // 本地 → 深绿
  mock:   "background:#4e342e;color:#ffcc80;",   // 模拟 → 棕橙
  codex:  "background:#0d47a1;color:#90caf9;",   // 云端 → 蓝
  claude: "background:#4a148c;color:#ce93d8;",   // 云端 → 紫
};

const STATUS_BORDER: Record<NodeStatus, string> = {
  idle:    "1px solid #4b5563",
  running: "2px solid #38bdf8",
  ok:      "2px solid #22c55e",
  error:   "3px solid #ef4444",   // ★ 红框高亮（配红光晕）
};

export const AgentNode = memo(function AgentNode(props: NodeProps) {
  const d = props.data as {
    label: string; agent: string; provider: string;
    tools: string[]; kind: string; task: string;
  };
  // 节点状态由画布层（FlowCanvas）按 WS 事件注入 props.data.status
  const status = ((props.data as { status?: NodeStatus }).status ?? "idle") as NodeStatus;

  return (
    <div
      style={{
        width: 240, minHeight: 96,
        border: STATUS_BORDER[status],
        borderRadius: 10,
        padding: "10px 12px",
        background: "#111827",
        color: "#e5e7eb",
        boxShadow: status === "error"
          ? "0 0 14px 2px rgba(239,68,68,.55)"        // 红光晕：红框的"高亮"语义
          : status === "running"
            ? "0 0 10px 1px rgba(56,189,248,.4)"
            : "0 1px 3px rgba(0,0,0,.5)",
        position: "relative",
        fontFamily: "ui-monospace, Consolas, monospace",
      }}
    >
      {/* 入端手柄（上游数据流）*/}
      <Handle type="target" position={Position.Top} style={{ background: "#38bdf8" }} />

      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 6 }}>
        {/* Provider 徽章 */}
        <span style={{
          ...parseStyle(PROVIDER_STYLE[d.provider] ?? "background:#374151;color:#d1d5db;"),
          fontSize: 11, padding: "1px 7px", borderRadius: 999,
          border: "1px solid rgba(255,255,255,.15)", fontWeight: 700,
        }}>
          {d.provider}
        </span>
        {/* kind 徽章（qa/review 节点额外标注 = 拦截判定器） */}
        {d.kind !== "agent" && (
          <span style={{
            fontSize: 10, padding: "1px 6px", borderRadius: 999,
            background: d.kind === "qa" ? "#7f1d1d" : "#422006", color: "#fca5a5",
          }}>
            {d.kind}
          </span>
        )}
        <span style={{ flex: 1, fontSize: 13, fontWeight: 700, color: "#f9fafb" }}>{d.label}</span>
      </div>

      <div style={{ fontSize: 11, color: "#9ca3af", overflow: "hidden", textOverflow: "ellipsis",
                    whiteSpace: "nowrap", marginBottom: 4 }}>
        {d.task}
      </div>

      <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
        {d.tools.map((t: string) => (
          <span key={t} style={{
            fontSize: 9, padding: "0 5px", borderRadius: 4,
            background: "#1f2937", color: "#d1d5db", border: "1px solid #374151",
          }}>
            {t}
          </span>
        ))}
      </div>

      {status === "error" && (
        <div style={{ marginTop: 4, fontSize: 10, color: "#fca5a5" }}>⛔ 拦截/失败 — 已红框标记</div>
      )}

      {/* 出端手柄（下游 / 行屏障） */}
      <Handle type="source" position={Position.Bottom} style={{ background: "#38bdf8" }} />
    </div>
  );
});

function parseStyle(s: string): React.CSSProperties {
  const out: Record<string, string> = {};
  for (const pair of s.split(";")) {
    const i = pair.indexOf(":");
    if (i > 0) out[pair.slice(0, i)] = pair.slice(i + 1).trim();
  }
  return out as React.CSSProperties;
}

export const nodeTypes = { agentNode: AgentNode as any };
