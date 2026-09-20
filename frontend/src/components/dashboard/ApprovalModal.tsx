/**
 * ApprovalModal.tsx —— Phase 3c：§1.5 人类审批断点小窗（human-in-the-loop）。
 *
 * 触发：点画布节点上的 ⚖ 需审批角标（store.approvalTarget = node_id）。
 * 显示：该节点 external_side 工具调用的 副作用分级 / 授权判定原因 / 所需 grant。
 * 动作：
 *   「批准」→ setApproval(nid, "approved")：签发审批断点令牌（本阶段记 approvalState，
 *            未来经 WS 引擎签发 token 并放行 external 调用，可回放）
 *   「拒绝」→ setApproval(nid, "rejected")：auth_block 留痕，节点保持受阻（大盘琥珀锁链）
 *
 * 铁律：全局 modal 挂载（App 层），不碰画布坐标。
 */
import { useWorkflowStore } from "../../store/workflowStore";

export default function ApprovalModal() {
  const target = useWorkflowStore((s) => s.approvalTarget);
  const auth = useWorkflowStore((s) => (s.approvalTarget ? s.authFlags[s.approvalTarget] : null));
  const approvalState = useWorkflowStore((s) => s.approvalState);
  const setApprovalTarget = useWorkflowStore((s) => s.setApprovalTarget);
  const setApproval = useWorkflowStore((s) => s.setApproval);

  if (!target || !auth) return null;
  const decided = approvalState[target];
  const btn = (c: string, bg: string, bd: string): React.CSSProperties => ({
    padding: "7px 18px", borderRadius: 8, fontSize: 12, fontWeight: 700,
    cursor: decided ? "not-allowed" : "pointer", opacity: decided ? 0.5 : 1,
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
          width: 420, background: "#0f172a", border: "1.5px solid #f59e0b",
          borderRadius: 14, padding: 18, color: "#e5e7eb",
          fontFamily: "ui-monospace, Consolas, monospace",
          boxShadow: "0 12px 40px rgba(0,0,0,.6)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
          <span style={{ fontSize: 20 }}>⚖</span>
          <b style={{ fontSize: 15, color: "#fcd34d" }}>人类审批断点（§1.5）</b>
          <span style={{ fontSize: 11, color: "#64748b", marginLeft: "auto" }}>节点 {target}</span>
        </div>

        <div style={{ fontSize: 11, lineHeight: 1.8, color: "#cbd5e1", marginBottom: 8 }}>
          <div>工具：<b style={{ color: "#f1f5f9" }}>{auth.tool || "—"}</b></div>
          <div>副作用分级：<b style={{ color: "#f59e0b" }}>{auth.sideEffect || "external_side"}</b>
            （带真实外部副作用：出外网 / 邮件 / 推远程 / 三方 API）</div>
          <div style={{ color: "#94a3b8", whiteSpace: "pre-wrap" }}>{auth.reason}</div>
        </div>

        {decided && (
          <div style={{
            fontSize: 11, marginBottom: 8, padding: "6px 10px", borderRadius: 8,
            background: decided === "approved" ? "#052e16" : "#450a0a",
            border: `1px solid ${decided === "approved" ? "#16a34a" : "#dc2626"}`,
            color: decided === "approved" ? "#4ade80" : "#fca5a5",
          }}>
            {decided === "approved"
              ? "✓ 已批准 —— 审批断点令牌签发（tool_authorize approval=…，可回放）"
              : "✗ 已拒绝 —— auth_block 留痕，节点保持受阻（锁链不解锁）"}
          </div>
        )}

        <div style={{ display: "flex", gap: 10, marginTop: 10 }}>
          <button style={btn("#4ade80", "#052e16", "#16a34a")}
            disabled={!!decided}
            onClick={() => setApproval(target, "approved")}>✓ 批准（签发断点）</button>
          <button style={btn("#fca5a5", "#450a0a", "#dc2626")}
            disabled={!!decided}
            onClick={() => setApproval(target, "rejected")}>✗ 拒绝</button>
          <button style={btn("#94a3b8", "transparent", "#334155")}
            onClick={() => setApprovalTarget(null)}>关闭</button>
        </div>
      </div>
    </div>
  );
}
