/** App 壳：左侧画布（React Flow + 行约束带）+ 右侧 LiveLogPanel（WS 事件时间线）。 */
import { useEffect, useState } from "react";
import FlowCanvas from "./components/canvas/FlowCanvas";
import LiveLogPanel from "./components/console/LiveLogPanel";
import { useWorkflowStore } from "./store/workflowStore";
import { connectRunSocket } from "./api/runSocket";
import type { Workflow } from "./lib/types";
import mockSample from "./sample/mock-demo.json";

export default function App() {
  const loadWorkflow = useWorkflowStore((s) => s.loadWorkflow);
  const onEvent = useWorkflowStore((s) => s.onEvent);
  const [wsStatus, setWsStatus] = useState<"connecting" | "open" | "closed">("connecting");

  // 1) 解析 JSON → 渲染画布（读本地样本；正式版 = fetch('../workflow.json')）
  useEffect(() => {
    loadWorkflow(mockSample as unknown as Workflow);
  }, [loadWorkflow]);

  // 2) 拉起引擎 + 接事件流：/run → POST，/ws/run → WS
  useEffect(() => {
    const dispose = connectRunSocket("ws://127.0.0.1:8790/ws/run", {
      onEvent,
      onStatus: (s) => setWsStatus(s === "open" ? "open" : s === "error" ? "connecting" : "closed"),
    });
    // 引擎已在跑则直接接流；"启动 run" 按钮走 API（见下方）
    return dispose;
  }, [onEvent]);

  const startRun = async () => {
    await fetch("http://127.0.0.1:8790/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workflow_id: "mock-demo", project_root: "." }),
    }).catch(() => {});
  };

  return (
    <div style={{ display: "flex", height: "100vh", background: "#0b1020", color: "#e5e7eb" }}>
      <div style={{ flex: 1, position: "relative" }}>
        <FlowCanvas />
        <div style={{
          position: "absolute", bottom: 12, left: 12, display: "flex", gap: 8, zIndex: 10,
        }}>
          <button
            onClick={startRun}
            style={{
              padding: "6px 14px", borderRadius: 8, border: "1px solid #334155",
              background: "#1e293b", color: "#7dd3fc", cursor: "pointer",
              fontFamily: "ui-monospace, monospace", fontSize: 12,
            }}
          >
            ▶ 启动 run（mock-demo）
          </button>
          <span style={{ fontSize: 11, color: "#64748b", alignSelf: "center", fontFamily: "ui-monospace, monospace" }}>
            ws: {wsStatus} · 保存 = 仅覆盖 workflow.json（kudosflow 式）
          </span>
        </div>
      </div>
      <LiveLogPanel />
    </div>
  );
}
