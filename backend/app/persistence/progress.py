"""
progress.md 持久化打卡（Strict Constraint #2）。

规则：所有 Agent 在执行实际代码/命令【之前】必须登记：
    时间(JST) | Agent/节点 | 已完成 | 当前动作 | 下一步

实现：向 <project_root>/progress.md 的 "## 打卡记录" 表格追加一行。
- 文件不存在 → 自动生成骨架（项目起点表头）；
- 已有表格 → 追加到表尾；已有 `## 打卡记录` 但无表 → 建表。
全部纯本地文件 IO，无锁（单进程引擎够用；多进程后续换 fcntl）。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")

_TABLE_HEADER = (
    "\n## 打卡记录\n\n"
    "| 时间 (JST) | Agent / 节点 | 已完成 | 当前动作 | 下一步 |\n"
    "|---|---|---|---|---|\n"
)


class ProgressLog:
    def __init__(self, project_root: str):
        self.path = Path(project_root) / "progress.md"

    def _ensure(self) -> None:
        if not self.path.exists():
            self.path.write_text(
                "# progress.md — 多智能体编排系统进度打卡\n\n"
                "> 规则：所有 Agent 执行实际代码/命令前，必须在此登记。\n"
                + _TABLE_HEADER,
                encoding="utf-8",
            )

    def register(
        self,
        agent_or_node: str,
        completed: str,
        current_action: str,
        next_step: str,
        ts: float | None = None,
    ) -> str:
        """登记一行打卡，返回写入的行文本（调用方会把它同时打进事件流）。"""
        self._ensure()
        stamp = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S") if ts is None \
            else datetime.fromtimestamp(ts, JST).strftime("%Y-%m-%d %H:%M:%S")
        row = f"| {stamp} | {agent_or_node} | {completed} | {current_action} | {next_step} |\n"
        text = self.path.read_text(encoding="utf-8")
        if "## 打卡记录" in text:
            self.path.write_text(text.rstrip() + "\n" + row, encoding="utf-8")
        else:
            self.path.write_text(text + _TABLE_HEADER + row, encoding="utf-8")
        return row.strip()

    # ---- 引擎侧的两个语义化埋点（executor 在节点前后各调一次） ----
    def node_enter(self, level: int, node_id: str, agent: str, next_step: str) -> None:
        self.register(f"L{level}:{node_id}({agent})", "调度进入", "执行节点任务（执行前打卡）", next_step)

    def node_exit(self, level: int, node_id: str, agent: str, completed: str) -> None:
        self.register(f"L{level}:{node_id}({agent})", completed, "执行完毕（执行后打卡）", "等待 QA/下一行")
