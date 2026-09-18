/**
 * levelLayout.ts —— ★ 同级节点同行（Strict Constraint #1）的坐标引擎。
 *
 * 核心不变量（与后端 validator 的"同行不变量"互为镜像）：
 *   节点的 Y 坐标【只】由它所在的 Level 决定 —— 同一行所有节点 Y 严格相等；
 *   同 X 坐标在一行内按 NODE_W + 间隙均匀分布（行内水平居中）；
 *   行间按 ROW_H 垂直步进。
 * 画布渲染、拖拽落位、行背景带，全部读这一份坐标，杜绝各算各的。
 */
import type { LevelDef, NodeDef } from "./types";

// ---- 布局常量（px，画布坐标系）----
export const NODE_W = 240;           // 节点宽
export const NODE_H = 96;            // 节点高
export const ROW_H = 190;            // 行高（节点高 + 行内留白）
export const GAP_X = 48;             // 同行节点水平间隙
export const ROW_LABEL_H = 28;       // 行左侧标签带高度
export const PADDING = 40;           // 画布四周留白

export interface LaidNode {
  nodeId: string;
  position: { x: number; y: number };   // React Flow 节点位置（左上角）
  rowY: number;                          // 本行基准 Y（用于画行背景带/对齐校验）
}

export interface LevelLayout {
  levelIndex: number;
  levelName: string;
  rowY: number;                 // ★ 本行所有节点的 Y（同一行 = 同一 rowY）
  rowHeight: number;
  nodes: LaidNode[];
  rowWidth: number;             // 本行内容宽度（背景带用）
}

/** 单行布局：行内 N 个节点 → 水平均匀分布，Y 锁死在 rowY。 */
export function layoutLevel(level: LevelDef, originX = PADDING, originY = PADDING): LevelLayout {
  const count = level.nodes.length;
  const totalW = count * NODE_W + (count - 1) * GAP_X;
  // 行内水平居中：该行内容相对 originX 偏移，让最左节点从 originX 起
  const rowY = PADDING + level.index * ROW_H;

  const nodes: LaidNode[] = level.nodes.map((nd: NodeDef, i) => ({
    nodeId: nd.id,
    position: {
      x: originX + i * (NODE_W + GAP_X),   // 均匀分布：i 号节点 = 起 + i*(宽+间隙)
      y: rowY,                               // ★ 同 Level 同 Y —— 不变量在这
    },
    rowY,
  }));

  return {
    levelIndex: level.index,
    levelName: level.name,
    rowY,
    rowHeight: ROW_H,
    nodes,
    rowWidth: totalW,
  };
}

/** 全工作流布局：levels[] → 逐行 LevelLayout[]（画布初始化 + 行背景带共用）。 */
export function layoutAll(levels: LevelDef[]): LevelLayout[] {
  return levels.map((lv) => layoutLevel(lv));
}

/**
 * 校验渲染结果是否守住"同级同行"不变量（前端侧自证，防手抖）：
 *  同一 rowY 的节点必须全属于同一 levelIndex。返回违规描述（空数组 = 通过）。
 */
export function assertSameRow(layouts: LevelLayout[]): string[] {
  const byRow = new Map<number, number>();
  for (const lv of layouts) {
    for (const n of lv.nodes) {
      const prev = byRow.get(n.rowY);
      if (prev !== undefined && prev !== lv.levelIndex) {
        return [`rowY=${n.rowY} 被 level ${prev} 与 level ${lv.levelIndex} 共用 —— 违反同级同行不变量`];
      }
      byRow.set(n.rowY, lv.levelIndex);
    }
  }
  return [];
}

export const labelOf = (nd: NodeDef, agents: Record<string, AgentDefLike>): string =>
  nd.label || agents[nd.agent]?.role || nd.agent;

// 轻量类型（避免把 AgentDef 全量引入布局模块）
export interface AgentDefLike {
  role?: string;
  provider?: string;
}
