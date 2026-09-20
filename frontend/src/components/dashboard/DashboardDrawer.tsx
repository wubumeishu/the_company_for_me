/**
 * DashboardDrawer.tsx —— Phase 3c：右侧"Git 实时流 / 站会大盘"抽屉。
 *
 * 默认"Git流"标签页：
 *   - PR 门禁记录卡片（数据 = store.prRecords ← pr_opened/pr_checks/pr_merged/pr_rejected）
 *   - 点卡片 → 就地展开详情（逐条 checkScripts ✓/✗ + red_flags 原始报错行 + merge_sha）
 *   - 终端流（git_commit + shell_log 事件，monospace 时间轴）
 * 节点焦点：点画布节点 → 顶部浮出该节点的 blocked 锁链 / redo 说明（drawerFocus）。
 *
 * 铁律：纯全局挂载件（App 层右侧栏），不碰 levelLayout 坐标、不碰 React Flow 画布。
 */
import { useMemo, useState } from "react";
import { useWorkflowStore } from "../../store/workflowStore";

type Tab = "git" | "burn" | "bugs" | "stall";

export default function DashboardDrawer() {
  const prRecords = useWorkflowStore((s) => s.prRecords);
  const gitCommits = useWorkflowStore((s) => s.gitCommits);
  const nodeBlocked = useWorkflowStore((s) => s.nodeBlocked);
  const redoNodes = useWorkflowStore((s) => s.redoNodes);
  const authFlags = useWorkflowStore((s) => s.authFlags);
  const fixAttempts = useWorkflowStore((s) => s.fixAttempts);
  const logEvents = useWorkflowStore((s) => s.logEvents);
  const expandedPr = useWorkflowStore((s) => s.expandedPr);
  const drawerFocus = useWorkflowStore((s) => s.drawerFocus);
  const setExpandedPr = useWorkflowStore((s) => s.setExpandedPr);
  const [tab, setTab] = useState<Tab>("git");

  const prs = useMemo(
    () => Object.values(prRecords).sort((a, b) => a.ts - b.ts),
    [prRecords],
  );
  // 终端流 = git_commit 事件（后端 git_* 流），取最后 30 条
  const commitLines = useMemo(
    () => gitCommits.slice(-30).map((c) =>
      `$ git_commit ${c.nodeId} @ ${c.branch}  ${c.sha.slice(0, 7)}  ${c.msg.slice(0, 40)}`),
    [gitCommits],
  );
  // shell_log 终端尾（venv 门禁脚本输出，3c 把"报错日志"直接给站会看）
  const shellTail = useMemo(() => {
    const out: string[] = [];
    for (let i = logEvents.length - 1; i >= 0 && out.length < 12; i--) {
      const ev = logEvents[i];
      if (ev.type === "shell_log" && ev.data?.cmd) {
        out.push(String(ev.data.cmd));
      }
    }
    return out.reverse();
  }, [logEvents]);

  const focusNode = drawerFocus ? nodeBlocked[drawerFocus] ?? null : null;
  const focusRedo = drawerFocus ? redoNodes[drawerFocus] : null;

  return (
    <div style={{
      width: 340, display: "flex", flexDirection: "column",
      background: "#0f172a", borderLeft: "1px solid #1e293b",
      fontFamily: "ui-monospace, Consolas, monospace",
    }}>
      {/* 标签行 */}
      <div style={{ display: "flex", borderBottom: "1px solid #1e293b" }}>
        {([
          ["git", "Git流"],
          ["burn", "燃尽"],
          ["bugs", "拦截Bug"],
          ["stall", "迟滞榜"],
        ] as [Tab, string][]).map(([k, label]) => (
          <button key={k}
            onClick={() => setTab(k)}
            style={{
              flex: 1, padding: "8px 4px", cursor: "pointer",
              background: "transparent", border: "none",
              borderBottom: tab === k ? "2px solid #f59e0b" : "2px solid transparent",
              color: tab === k ? "#fde68a" : "#64748b",
              fontSize: 11, fontWeight: tab === k ? 700 : 400,
              fontFamily: "inherit",
            }}
          >{label}</button>
        ))}
      </div>

      <div style={{ padding: 10, overflowY: "auto", flex: 1 }}>
        {/* 节点焦点浮层：点画布节点 → 该节点的 blocked 锁链 / redo 说明 */}
        {drawerFocus && (focusNode || focusRedo) && (
          <div style={{
            marginBottom: 10, padding: "8px 10px", borderRadius: 8,
            background: focusNode ? "#1c1a0e" : "#0f2550",
            border: `1px solid ${focusNode ? "#f59e0b" : "#2563eb"}`,
            fontSize: 11, color: focusNode ? "#fcd34d" : "#93c5fd",
          }}>
            <b>◉ {drawerFocus}</b>
            {focusNode && (
              <div style={{ marginTop: 4 }}>
                ⛓ 锁链 blocked_by: {focusNode.blockedBy.join(", ") || "—"}（{focusNode.reason}）
                <div style={{ fontSize: 10, opacity: .7 }}>解锁条件 = 上游转绿（派生态自动消）</div>
              </div>
            )}
            {focusRedo && <div style={{ marginTop: 4 }}>🚩 {focusRedo}</div>}
          </div>
        )}

        {tab === "git" && (
          <>
            <div style={{ color: "#94a3b8", fontWeight: 700, marginBottom: 8, fontSize: 11 }}>
              PR 门禁记录（{prs.length}）
            </div>
            {prs.length === 0 && <div style={{ color: "#475569", fontSize: 11, marginBottom: 8 }}>暂无 PR（等 pr_opened 事件…）</div>}
            {prs.map((pr) => (
              <PrCard key={pr.prId} pr={pr}
                expanded={expandedPr === pr.prId}
                onToggle={() => setExpandedPr(expandedPr === pr.prId ? null : pr.prId)} />
            ))}

            <div style={{ color: "#94a3b8", fontWeight: 700, marginTop: 12, marginBottom: 6, fontSize: 11 }}>
              Git 实时流（提交证据）
            </div>
            <div style={{
              background: "#020617", borderRadius: 8, padding: 8,
              border: "1px solid #1e293b", maxHeight: 160, overflowY: "auto",
              fontSize: 10, lineHeight: 1.6, color: "#4ade80",
            }}>
              {commitLines.length === 0 && <div style={{ color: "#475569" }}>等待 git_commit 事件…</div>}
              {commitLines.map((l, i) => <div key={i}>{l}</div>)}
              {shellTail.length > 0 && (
                <>
                  <div style={{ color: "#64748b", margin: "6px 0 2px", borderTop: "1px dashed #334155", paddingTop: 4 }}>venv 门禁脚本输出</div>
                  {shellTail.map((l, i) => (
                    <div key={i} style={{ color: "#94a3b8", whiteSpace: "pre-wrap" }}>{l}</div>
                  ))}
                </>
              )}
            </div>
          </>
        )}

        {tab === "burn" && <StandupStat
          title="燃尽（节点收编）"
          value={`${Object.values(nodeBlocked).length === 0 ? "全通" : "受阻中"}`}
          sub={`${Object.keys(nodeBlocked).length} 节点处于锁链 · ${fixAttempts.length} 次回滚`}
        />}
        {tab === "bugs" && <StandupStat
          title="拦截 Bug（红灯明细）"
          value={`${prs.filter((p) => p.state === "rejected").length}`}
          sub={prs.flatMap((p) => p.redFlags).slice(0, 4).join(" · ") || "暂无 red_flags"}
        />}
        {tab === "stall" && <StandupStat
          title="迟滞榜（审批/挂起）"
          value={`${Object.values(authFlags).filter((a) => !a.allowed).length} 个需人工审批`}
          sub={Object.entries(authFlags).filter(([, a]) => !a.allowed)
            .map(([nid, a]) => `${nid}:${a.tool}`).join(" · ") || "无待审批 external 调用"}
        />}
      </div>
    </div>
  );
}

/** PR 卡片：merged=绿 / rejected=红 / open=蓝；点击就地展开详情（逐条 checks + red_flags） */
function PrCard({ pr, expanded, onToggle }: {
  pr: { prId: string; state: "open" | "merged" | "rejected"; sourceBranches: string[];
         target?: string; checks: { name: string; passed: boolean; detail: string }[];
         redFlags: string[]; mergeSha?: string; ts: number };
  expanded: boolean; onToggle: () => void;
}) {
  const border = pr.state === "merged" ? "#16a34a" : pr.state === "rejected" ? "#dc2626" : "#2563eb";
  const label = pr.state === "merged" ? "merged" : pr.state === "rejected" ? "rejected" : "open";
  return (
    <div onClick={onToggle} style={{
      border: `1px solid ${expanded ? border : "#334155"}`,
      background: expanded ? "#111827" : "#0f172a",
      borderRadius: 8, padding: "8px 10px", marginBottom: 8, cursor: "pointer",
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12 }}>
        <b style={{ color: "#f1f5f9" }}>{pr.prId}</b>
        <span style={{
          fontSize: 10, padding: "1px 7px", borderRadius: 999, fontWeight: 700,
          background: border, color: "#fff",
        }}>{label}</span>
        <span style={{ fontSize: 10, color: "#64748b", marginLeft: "auto" }}>{expanded ? "▲ 收起" : "▼ 展开"}</span>
      </div>
      <div style={{ fontSize: 10, color: "#64748b", marginTop: 3 }}>
        源分支 {pr.sourceBranches.join(", ") || "—"}{pr.target ? ` → ${pr.target}` : ""}
      </div>
      {expanded && (
        <div style={{ marginTop: 6, fontSize: 10, lineHeight: 1.7 }}>
          {pr.checks.map((c, i) => (
            <div key={i} style={{ color: c.passed ? "#4ade80" : "#f87171" }}>
              {c.passed ? "✓" : "✗"} {c.name} — {c.detail}
            </div>
          ))}
          {pr.redFlags.length > 0 && (
            <div style={{ marginTop: 4, color: "#fca5a5", whiteSpace: "pre-wrap" }}>
              <b>red_flags:</b> {pr.redFlags.join("\n")}
            </div>
          )}
          {pr.mergeSha && <div style={{ color: "#4ade80", marginTop: 4 }}>merge_sha {pr.mergeSha.slice(0, 12)}</div>}
        </div>
      )}
    </div>
  );
}

/** 站会四屏占位（燃尽/拦截Bug/迟滞榜 —— 3c 尾接入 datastore 后出真数字） */
function StandupStat({ title, value, sub }: { title: string; value: string; sub: string }) {
  return (
    <div style={{ background: "#111827", border: "1px solid #334155", borderRadius: 8,
                  padding: 12, fontSize: 11, color: "#e5e7eb" }}>
      <div style={{ color: "#94a3b8", fontSize: 10, marginBottom: 4 }}>{title}</div>
      <div style={{ fontSize: 18, fontWeight: 700, color: "#fde68a" }}>{value}</div>
      <div style={{ marginTop: 4, color: "#64748b", whiteSpace: "pre-wrap" }}>{sub}</div>
    </div>
  );
}
