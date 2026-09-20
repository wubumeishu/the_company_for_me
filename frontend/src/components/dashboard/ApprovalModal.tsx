/**
 * ApprovalModal.tsx —— Phase 3c 尾②：§1.5 人类审批断点小窗（human-in-the-loop 真通路）。
 *
 * 触发源（store.approvalTarget，语义 = request_id 或 node_id）：
 *   ① 引擎挂起：approval_request 事件 → store 自动把 approvalTarget 设成 request_id（小窗自动弹）
 *   ② 手动点画布 ⚖ 角标：AgentNode 调 setApprovalTarget(node_id)（回看某个被拒节点）
 * 显示：该 external_side 调用的 工具 / 副作用分级 / 授权判定原因（来自 pendingApprovals 或 authFlags）。
 * 动作：
 *   「批准」→ decideApproval(requestId, "approve") → POST /approval → 引擎签 HMAC 令牌放行挂起引擎
 *   「拒绝」→ decideApproval(requestId, "deny")   → 引擎 auth_block 留痕 + 节点保持受阻
 *   无挂起请求（纯回看）→ 只显示留痕态（approvalState），按钮禁用。
 *
 * 铁律：全局 modal 挂载（App 层），不碰画布坐标。
 */
import { useWorkflowStore } from "../../store/workflowStore";

export default function ApprovalModal() {
  const target = useWorkflowStore((s) => s.approvalTarget);
  const pending = useWorkflowStore((s) => s.pendingApprovals);
  const authFlags = useWorkflowStore((s) => s.authFlags);
  const approvalState = useWorkflowStore((s) => s.approvalState);
  const setApprovalTarget = useWorkflowStore((s) => s.setApprovalTarget);
  const decideApproval = useWorkflowStore((s) => s.decideApproval);

  if (!target) return null;

  // 解析出这条小窗对应的挂起审批（按 request_id 命中，或按 node_id 反查 pending）
  const req = pending[target] ?? Object.values(pending).find((p) => p.nodeId === target) ?? null;
  // 回看态：无挂起请求时，落 authFlags（历史 ⚖ 判定）供"看看当时为什么被拒"
  const flag = authFlags[target];
  const nodeId = req ? req.nodeId : target;
  const tool = req ? req.tool : (flag?.tool ?? "");
  const sideEffect = req ? req.sideEffect : (flag?.sideEffect ?? "external_side");
  const reason = req ? req.reason : (flag?.reason ?? "");
  const decided = approvalState[nodeId];            // "approved" | "rejected" | undefined
  const isPending = req != null;

  const btn = (c: string, bg: string, bd: string, disabled: boolean): React.CSSProperties => ({
    padding: "7px 18px", borderRadius: 8, fontSize: 12, fontWeight: 700,
    cursor: disabled ? "not-allowed" : "pointer", opacity: disabled ? 0.5 : 1,
    background: bg, color: c, border: `1.5px solid ${bd}`, fontFamily: "ui-monospace, monospace",
  });

  return (
    <div style={{
      position: "fixed", inset: 0, zIndex: 100, display: "flex",
      alignItems: "center", justifyContent: "center", background: "rgba(2,6,23,.72)",
    }}
      onClick={() => setApprovalTarget(null)}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 440, background: "#0f172a",
          border: `1.5px solid ${isPending ? "#f59e0b" : "#334155"}`,
          borderRadius: 14, padding: 18, color: "#e5e7eb",
          fontFamily: "ui-monospace, Consolas, monospace",
          boxShadow: "0 12px 40px rgba(0,0,0,.6)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
          <span style={{ fontSize: 20 }}>{isPending ? "⚖" : "📋"}</span>
          <b style={{ fontSize: 15, color: isPending ? "#fcd34d" : "#94a3b8" }}>
            {isPending ? "人类审批断点（§1.5 公章待签）" : "审批留痕（回看）"}
          </b>
          <span style={{ fontSize: 11, color: "#64748b", marginLeft: "auto" }}>节点 {nodeId}</span>
        </div>

        <div style={{ fontSize: 11, lineHeight: 1.8, color: "#cbd5e1", marginBottom: 8 }}>
          <div>工具：<b style={{ color: "#f1f5f9" }}>{tool || "—"}</b></div>
          <div>副作用分级：<b style={{ color: "#f59e0b" }}>{sideEffect}</b>
            （带真实外部副作用：出外网 / 邮件 / 推远程 / 三方 API）</div>
          <div style={{ color: "#94a3b8", whiteSpace: "pre-wrap" }}>{reason}</div>
          {isPending && (
            <div style={{ marginTop: 4, color: "#fbbf24" }}>
              挂起中 — 批准后引擎签 HMAC 令牌（nonce 一次性 + TTL）放行；拒绝/超时则保持受阻。
            </div>
          )}
        </div>

        {decided && (
          <div style={{
            fontSize: 11, marginBottom: 8, padding: "6px 10px", borderRadius: 8,
            background: decided === "approved" ? "#052e16" : "#450a0a",
            border: `1px solid ${decided === "approved" ? "#16a34a" : "#dc2626"}`,
            color: decided === "approved" ? "#4ade80" : "#fca5a5",
          }}>
            {decided === "approved"
              ? "✓ 已批准 —— 审批断点令牌签发（tool_authorize approval=…，auth_grant 留账）"
              : "✗ 已拒绝 —— auth_block 留痕，节点保持受阻（锁链不解锁）"}
          </div>
        )}

        <div style={{ display: "flex", gap: 10, marginTop: 10 }}>
          <button style={btn("#4ade80", "#052e16", "#16a34a", !isPending || !!decided)}
            disabled={!isPending || !!decided}
            onClick={() => req && decideApproval(req.requestId, "approve")}>✓ 批准（签发断点）</button>
          <button style={btn("#fca5a5", "#450a0a", "#dc2626", !isPending || !!decided)}
            disabled={!isPending || !!decided}
            onClick={() => req && decideApproval(req.requestId, "deny")}>✗ 拒绝</button>
          <button style={btn("#94a3b8", "transparent", "#334155", false)}
            onClick={() => setApprovalTarget(null)}>关闭</button>
        </div>
      </div>
    </div>
  );
}
