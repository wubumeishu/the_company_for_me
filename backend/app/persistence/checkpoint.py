"""checkpoint：行级快照，支撑 QA 失败后的"定向回退 + 行内重跑"（而非全量重启）。

落地文件：<project_root>/.company/checkpoint_<run_id>.json（.company/ 已 gitignore）。
记录：已完成的 (level, node) 结果 + 各节点产物（outputs 文本）+ 回退目标。
回退时引擎据此跳过已通过的上游行，从 targetLevel 起重跑该行节点。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class NodeResult:
    level: int
    node_id: str
    ok: bool
    outputs: dict[str, str] = field(default_factory=dict)   # name -> artifact text
    error: Optional[str] = None
    finished_at: float = 0.0


class CheckpointStore:
    def __init__(self, project_root: str, run_id: str):
        self.path = Path(project_root) / ".company" / f"checkpoint_{run_id}.json"

    def save(self, results: dict[str, NodeResult], rollback_target: Optional[int] = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "rollback_target": rollback_target,
            "nodes": {k: {
                "node_id": r.node_id, "level": r.level, "ok": r.ok, "outputs": r.outputs,
                "error": r.error, "finished_at": r.finished_at,
            } for k, r in results.items()},
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def load(self) -> Optional[dict[str, Any]]:
        if not self.path.exists():
            return None
        return json.loads(self.path.read_text(encoding="utf-8"))

    @staticmethod
    def can_skip(result: NodeResult) -> bool:
        """回退重跑时：上游已通过且目标行之前的节点不重跑（keepCheckpoint 语义）。"""
        return result.ok and result.error is None
