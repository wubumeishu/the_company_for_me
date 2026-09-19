/**
 * AgentNode.tsx —— 自定义节点：角色/工具徽章 + Provider 徽章 + 失败红框。
 *
 * 样式约定（Strict Constraint #3 的可视化落地）：
 *   idle    → 灰边
 *   running → 蓝边 + 呼吸光
 *   ok      → 绿边
 *   error   → ★ 外边框 2px 红 + 红光晕（流转图红框高亮，一眼看到拦截点）
 * Provider 徽章（Ollama/Codex/Claude/Mock）用不同底色区分本地 vs 云端。
 *
 * ★ Phase 2c 工位视觉升级（任务2）：
 *   ① 案卷堆叠：高度比例 = clamp(estimate/12000, 0.2, 1.0)，estimate 取实时状态流
 *      （load_change 事件 → store.nodeLoad；真实 LLM 后 usage_settle 回填"实耗"）
 *   ② 咖啡厅冷却皮肤：agent 状态 RESTING（限流/体力耗尽）→ 暖棕皮肤 + ☕ +
 *      环形倒计时（数据 = 后端 rate_limit_hit 的 resume_at → 本地 500ms 平滑倒数）
 *   ③ 部门负责人标识：head → 金色「★ 部长」徽章 + 金描边（/org_chart flat.head 注入）
 *
 * 铁律：所有新增视觉都在节点【内部】渲染，不碰 levelLayout 坐标 —— 同级同行不变量零风险。
 */
import { memo, useEffect, useRef, useState } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { NodeStatus } from "../../lib/types";
import { useWorkflowStore } from "../../store/workflowStore";

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

interface NodeData {
  label: string; agent: string; provider: string;
  tools: string[]; kind: string; task: string;
  status?: NodeStatus;
  tokenBudget?: { estimate?: number };   // workflow.json 静态声明（缺实时事件时的兜底源）
  head?: boolean;
  department?: string | null;
}

export const AgentNode = memo(function AgentNode(props: NodeProps) {
  const d = props.data as unknown as NodeData;
  // 节点状态由画布层（FlowCanvas）按 WS 事件注入 props.data.status
  const status = (d.status ?? "idle") as NodeStatus;

  // ★ 2c ①②③：实时状态流（store 事件驱动，节点内部重渲染，坐标零改动）
  const load = useWorkflowStore((s) => s.nodeLoad[props.id]);
  const res = useWorkflowStore((s) => s.agentRes[d.agent]);
  const resting = res?.state === "RESTING";

  // ---- ① 案卷堆叠：高度比例 = clamp(estimate/12000, 0.2, 1.0)（铁律映射）----
  const estimate = load?.estimate ?? d.tokenBudget?.estimate ?? 0;
  const ratio = estimate > 0 ? Math.min(1, Math.max(0.2, estimate / 12000)) : 0.2;
  const stackH = Math.round(10 + ratio * 22);          // 像素高度 12–32px（视觉缩放）

  // ---- ② 咖啡厅冷却：环形倒计时（后端 resume_at 权威，本地 500ms 平滑倒数）----
  const [left, setLeft] = useState(0);
  const endRef = useRef(0);
  useEffect(() => {
    if (!resting) { setLeft(0); return; }
    endRef.current = Date.now() / 1000 + (res?.remainingSec ?? 0);
    const iv = window.setInterval(() => {
      const r = Math.max(0, endRef.current - Date.now() / 1000);
      setLeft(r);
      if (r <= 0) window.clearInterval(iv);
    }, 500);
    return () => window.clearInterval(iv);
  }, [resting, res?.remainingSec]);
  const frac = resting && (res?.restTotal ?? 0) > 0 ? left / (res?.restTotal ?? 1) : 0;

  // ---- ② 咖啡厅皮肤（RESTING 切底色/边框；非 RESTING 保持原语义零变化）----
  const bg = resting
    ? "linear-gradient(160deg,#3b2a1a 0%,#2a1e12 100%)"
    : status === "error" ? "#1f1215" : "#111827";
  const border = resting && status !== "error" ? "2px solid #d97706" : STATUS_BORDER[status];
  const shadow = status === "error"
    ? "0 0 14px 2px rgba(239,68,68,.55)"
    : resting ? "0 0 12px 1px rgba(217,119,6,.45)"
      : status === "running" ? "0 0 10px 1px rgba(56,189,248,.4)"
      : "0 1px 3px rgba(0,0,0,.5)";
  const head = d.head === true;

  return (
    <div style={{
      width: 240, minHeight: 96,
      border, borderRadius: 10, padding: "10px 12px",
      background: bg,
      color: resting ? "#fde68a" : "#e5e7eb",
      boxShadow: shadow,
      position: "relative",
      fontFamily: "ui-monospace, Consolas, monospace",
      outline: head ? "2px solid #fbbf24" : "none",   // ★ ③ head 金描边（内层，不占布局）
      outlineOffset: 2,
    }}>
      {/* 入端手柄（上游数据流）*/}
      <Handle type="target" position={Position.Top} style={{ background: "#38bdf8" }} />

      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 6 }}>
        {/* Provider 徽章（RESTING 时前置咖啡杯图标） */}
        <span style={{
          ...parseStyle(PROVIDER_STYLE[d.provider] ?? "background:#374151;color:#d1d5db;"),
          fontSize: 11, padding: "1px 7px", borderRadius: 999,
          border: "1px solid rgba(255,255,255,.15)", fontWeight: 700,
        }}>
          {resting ? "☕ " : ""}{d.provider}
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
        {/* ★ ③ 部门负责人徽章 */}
        {head && (
          <span style={{
            fontSize: 10, padding: "1px 6px", borderRadius: 999, fontWeight: 700,
            background: "#78350f", color: "#fde68a", border: "1px solid #fbbf24",
          }}>
            ★ 部长{d.department ? `·${d.department}` : ""}
          </span>
        )}
        <span style={{ flex: 1, fontSize: 13, fontWeight: 700,
                        color: resting ? "#fde68a" : "#f9fafb" }}>{d.label}</span>
      </div>

      <div style={{ fontSize: 11, color: resting ? "#d6b370" : "#9ca3af",
                    overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                    marginBottom: 4 }}>
        {resting ? "☕ 咖啡厅小坐中…" : d.task}
      </div>

      {/* ★ ① 案卷堆叠（高度=clamp(estimate/12000,.2,1.0)） + 工具徽章同行 */}
      <div style={{ display: "flex", gap: 6, alignItems: "flex-end" }}>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center",
                      justifyContent: "flex-end", width: 34, flexShrink: 0 }}>
          <div style={{ width: 24, height: stackH, borderRadius: 3,
                        background: resting ? "#92400e" : "#1f2937",
                        border: "1px solid " + (resting ? "#b45309" : "#4b5563"),
                        position: "relative", overflow: "hidden" }}>
            {[0, 1, 2, 3].map((i) => (
              <div key={i} style={{ position: "absolute", left: 2, right: 2,
                                    top: 2 + i * (stackH - 4) / 4, height: 1.5,
                                    background: resting ? "rgba(253,230,138,.5)" : "rgba(148,163,184,.55)" }} />
            ))}
          </div>
          <span style={{ fontSize: 8, color: "#64748b", marginTop: 2 }}>
            {load?.actual != null ? `实耗${load.actual}` : estimate > 0 ? `${Math.round(estimate / 1000)}k` : "案卷"}
          </span>
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4, flex: 1, minHeight: 12 }}>
          {d.tools.slice(0, 4).map((t: string) => (
            <span key={t} style={{
              fontSize: 9, padding: "0 5px", borderRadius: 4,
              background: "#1f2937", color: "#d1d5db", border: "1px solid #374151",
            }}>
              {t}
            </span>
          ))}
        </div>
      </div>

      {/* ★ ② 咖啡厅环形倒计时（RESTING 才显示） */}
      {resting && <CoffeeRing left={left} frac={frac} total={res?.restTotal ?? 0} />}

      {status === "error" && (
        <div style={{ marginTop: 4, fontSize: 10, color: "#fca5a5" }}>⛔ 拦截/失败 — 已红框标记</div>
      )}

      {/* 出端手柄（下游 / 行屏障） */}
      <Handle type="source" position={Position.Bottom} style={{ background: "#38bdf8" }} />
    </div>
  );
});

/** 环形倒计时：剩余秒 + 弧长比例（后端 resume_at 权威，前端本地平滑倒数）。 */
function CoffeeRing({ left, frac, total }: { left: number; frac: number; total: number }) {
  const R = 13, C = 2 * Math.PI * R;
  const sec = Math.ceil(left);
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 8 }}>
      <svg width={R * 2 + 6} height={R * 2 + 6} style={{ flexShrink: 0 }}>
        <circle cx={R + 3} cy={R + 3} r={R} fill="none" stroke="#451a03" strokeWidth={4} />
        <circle cx={R + 3} cy={R + 3} r={R} fill="none" stroke="#f59e0b" strokeWidth={4}
                strokeDasharray={C} strokeDashoffset={C * (1 - Math.max(0, Math.min(1, frac)))}
                strokeLinecap="round" transform={`rotate(-90 ${R + 3} ${R + 3})`} />
        <text x={R + 3} y={R + 6} textAnchor="middle" fontSize={9} fill="#fde68a">☕</text>
      </svg>
      <div style={{ fontSize: 10, color: "#fbbf24", lineHeight: 1.5 }}>
        <div>冷却剩余 <b>{total > 0 ? `${sec}s` : "…"}</b></div>
        <div style={{ color: "#92400e", fontSize: 9 }}>回岗自动唤醒（tick）</div>
      </div>
    </div>
  );
}

function parseStyle(s: string): React.CSSProperties {
  const out: Record<string, string> = {};
  for (const pair of s.split(";")) {
    const i = pair.indexOf(":");
    if (i > 0) out[pair.slice(0, i)] = pair.slice(i + 1).trim();
  }
  return out as React.CSSProperties;
}

export const nodeTypes = { agentNode: AgentNode as any };
