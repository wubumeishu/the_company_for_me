/**
 * RowConstraint.tsx —— 行的视觉约束层（同级同行的"看得见"）。
 *
 * 每个 Level 画一条水平背景带 + 左侧行标签（L0/L1/L2 + gate）：
 *   · 背景带顶/底 = 行 y 坐标 ± 半行高（全部来自 levelLayout，唯一坐标源）
 *   · 行内节点 Y 严格锁死在带内中线（layoutLevel 保证）
 *   · 若 assertSameRow 报错（布局被手抖打破），整行闪红提示
 */
import { memo } from "react";
import type { LevelLayout } from "../../lib/levelLayout";

export const RowConstraint = memo(function RowConstraint({
  layout, violation,
}: {
  layout: LevelLayout;
  violation: boolean;
}) {
  // 背景带：从该行第一个节点的 y 起，横向覆盖整行内容宽度
  const x = 16;
  const w = layout.rowWidth + 32;
  const y = layout.rowY - 14;
  const h = layout.rowHeight - 8;

  return (
    <div
      style={{
        position: "absolute",
        left: x, top: y, width: w, height: h,
        background: violation ? "rgba(239,68,68,.06)" : "rgba(56,189,248,.05)",
        border: violation ? "1px dashed #ef4444" : "1px dashed rgba(56,189,248,.25)",
        borderRadius: 8,
        pointerEvents: "none",
        zIndex: 0,
      }}
    >
      <span
        style={{
          position: "absolute", top: 4, left: 8,
          fontSize: 10, fontWeight: 700, letterSpacing: 1,
          color: violation ? "#fca5a5" : "#7dd3fc",
          fontFamily: "ui-monospace, Consolas, monospace",
        }}
      >
        L{layout.levelIndex} {layout.levelName} · 并发×{layout.nodes.length}
        {violation ? " ⚠同行不变量被打破" : ""}
      </span>
    </div>
  );
});
