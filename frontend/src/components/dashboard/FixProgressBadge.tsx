/**
 * FixProgressBadge.tsx —— Phase 3c：顶栏"纠错进度 x/3"实心/空心圆点徽标。
 *
 * 数据源 = store.fixAttempts（← rollback 事件的 attempt 计数，拍板 #3 = maxAttempts=3）。
 *   实心点 ● = 已用纠错次数；空心点 ○ = 剩余额度；上限固定 3（workflow qa.retry.maxAttempts）。
 * 悬停（title）展开每次回滚的 from→to + 序号。
 *
 * 铁律：纯顶栏挂载件（全局），不碰画布坐标。
 */
import { useMemo } from "react";
import { useWorkflowStore } from "../../store/workflowStore";

const MAX = 3;   // 拍板 #3：maxAttempts=3（与 orchestrator 默认一致）

export default function FixProgressBadge() {
  const attempts = useWorkflowStore((s) => s.fixAttempts);
  const used = Math.min(attempts.length, MAX);
  const exhausted = attempts.length >= MAX;

  const tooltip = useMemo(
    () => attempts.length === 0
      ? "纠错回路：无回滚（跑全绿）"
      : attempts.map((a, i) => `第${i + 1}次 回滚 L${a.fromLevel}→L${a.toLevel}`).join("；")
        + (exhausted ? "；3 次耗尽（彻底 FAILED）" : ""),
    [attempts, exhausted],
  );

  return (
    <span
      title={`纠错进度（maxAttempts=${MAX}）\n${tooltip}`}
      style={{
        display: "inline-flex", alignItems: "center", gap: 7,
        padding: "4px 12px", borderRadius: 999,
        border: `1.5px dashed ${exhausted ? "#dc2626" : used > 0 ? "#f59e0b" : "#334155"}`,
        background: exhausted ? "#450a0a" : used > 0 ? "#451a03" : "#0f172a",
        fontSize: 11, fontWeight: 700,
        color: exhausted ? "#fca5a5" : used > 0 ? "#fcd34d" : "#64748b",
        fontFamily: "ui-monospace, Consolas, monospace",
        cursor: "default",
      }}
    >
      纠错 {used}/{MAX}
      {/* 实心/空心圆点：实心=已用，空心=剩余 */}
      <span style={{ display: "inline-flex", gap: 4, alignItems: "center" }}>
        {Array.from({ length: MAX }, (_, i) => (
          <span key={i} style={{
            width: 9, height: 9, borderRadius: "50%",
            background: i < used
              ? (exhausted ? "#dc2626" : "#f59e0b")
              : "transparent",
            border: `1.5px solid ${i < used ? (exhausted ? "#dc2626" : "#f59e0b") : "#475569"}`,
          }} />
        ))}
      </span>
      {exhausted && <span style={{ fontSize: 10 }}>⛔ 耗尽</span>}
    </span>
  );
}
