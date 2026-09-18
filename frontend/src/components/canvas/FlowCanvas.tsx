/**
 * FlowCanvas.tsx —— ★ React Flow 画布主组件。
 *
 * 渲染管线（Task 2 全链路）：
 *   workflow.json → store.loadWorkflow → layoutAll(levels) 算坐标
 *   → toReactFlow 生成节点/边 → ReactFlow 渲染（位置受控，节点锁死同行 Y）
 *   → /ws/run 事件 → store.onEvent → 节点 data.status 刷新（红框出现）
 *
 * 铁律：节点位置【只读】（拖动会被弹回）——画布是"展示与保存"，
 * 位置唯一来源是 levelLayout（同级同行不变量的前端背书）。
 */
import { useCallback, useEffect, useMemo } from "react";
import {
  ReactFlow, ReactFlowProvider, Background, Controls, MiniMap,
  useNodesState, useEdgesState, type Node, type Edge, type OnNodeDrag,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import { useWorkflowStore, toReactFlow } from "../../store/workflowStore";
import { nodeTypes } from "./AgentNode";
import { RowConstraint } from "./RowConstraint";
import {
  assertSameRow, PADDING, NODE_W, ROW_H, type LevelLayout,
} from "../../lib/levelLayout";

export default function FlowCanvas() {
  return (
    <ReactFlowProvider>
      <div style={{ width: "100%", height: "100%" }}>
        <CanvasInner />
      </div>
    </ReactFlowProvider>
  );
}

function CanvasInner() {
  const workflow = useWorkflowStore((s) => s.workflow);
  const layouts = useWorkflowStore((s) => s.layouts);
  const nodeStatus = useWorkflowStore((s) => s.nodeStatus);

  const violations = useMemo(() => assertSameRow(layouts), [layouts]);

  // 初始节点/边：由 workflow + 坐标布局生成（非自由图）
  const { nodes: initNodes, edges: initEdges } = useMemo(
    () => (workflow ? toReactFlow(workflow, layouts) : { nodes: [], edges: [] }),
    [workflow, layouts],
  );

  const [nodes, setNodes, onNodesChange] = useNodesState(initNodes as Node[]);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initEdges as Edge[]);

  // workflow 变化 → 重铺（行布局全量重算）
  useEffect(() => {
    if (workflow) {
      const { nodes: n, edges: e } = toReactFlow(workflow, layouts);
      setNodes(n as Node[]);
      setEdges(e as Edge[]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflow, layouts]);

  // WS 事件 → 节点 data.status 刷新（红框/绿框/呼吸蓝在这里落点）
  useEffect(() => {
    setNodes((prev) =>
      prev.map((n) => {
        const st = nodeStatus[n.id];
        if (!st || n.data?.status === st) return n;
        return { ...n, data: { ...n.data, status: st } };
      }),
    );
  }, [nodeStatus]);

  // ★ 位置受控：拖动节点后弹回 levelLayout 坐标（同行不变量的运行时守卫）
  const onNodeDragStop: OnNodeDrag<Node> = useCallback(
    (_e, _n, _ns) => {
      const pos = layoutPositions(layouts);
      setNodes((prev) => prev.map((n) => (pos[n.id] ? { ...n, position: pos[n.id] } : n)));
    },
    [layouts, setNodes],
  );

  // 画布尺寸 = 内容尺寸（行布局决定，min 一屏）
  const canvasW = useMemo(() => {
    let maxX = 0;
    for (const lv of layouts) for (const n of lv.nodes) maxX = Math.max(maxX, n.position.x + NODE_W);
    return maxX + PADDING * 2;
  }, [layouts]);
  const canvasH = useMemo(
    () => (layouts.length ? PADDING * 2 + layouts.length * ROW_H : 600),
    [layouts],
  );

  return (
    <div style={{ position: "relative", width: "100%", height: "100%", background: "#0b1020" }}>
      {/* 行约束背景带（画在 ReactFlow 之下） */}
      {layouts.map((lv) => (
        <RowConstraint key={lv.levelIndex} layout={lv} violation={violations.length > 0} />
      ))}
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeDragStop={onNodeDragStop}
        fitView
        minZoom={0.3}
        maxZoom={2.5}
        proOptions={{ hideAttribution: true }}
        onlyRenderVisibleElements
        defaultEdgeOptions={{ style: { stroke: "#475569", strokeWidth: 1.5 } }}
      >
        <Background gap={24} size={1} color="#1e293b" />
        <Controls />
        <MiniMap
          nodeColor={(n) => {
            const st = (n.data as any)?.status;
            return st === "error" ? "#ef4444" : st === "ok" ? "#22c55e" : st === "running" ? "#38bdf8" : "#475569";
          }}
          style={{ background: "#0b1020" }}
        />
      </ReactFlow>

      {violations.length > 0 && (
        <div style={{
          position: "absolute", top: 10, left: "50%", transform: "translateX(-50%)",
          background: "#7f1d1d", color: "#fecaca", padding: "6px 14px",
          borderRadius: 8, fontSize: 12, zIndex: 10, fontFamily: "ui-monospace, monospace",
        }}>
          ⚠ {violations[0]}
        </div>
      )}
    </div>
  );
}

/** 全量节点坐标表（levelLayout 是唯一坐标源，拖拽弹回用它） */
function layoutPositions(layouts: LevelLayout[]) {
  const out: Record<string, { x: number; y: number }> = {};
  for (const lv of layouts) for (const n of lv.nodes) out[n.nodeId] = n.position;
  return out;
}
