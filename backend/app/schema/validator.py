"""
数据契约加载 + 校验 + 同行不变量。

这是整个系统的"生死线"：前端画布保存下来的 workflow.json 进引擎前，
必须在这里过三关——
  1) JSON Schema (draft-07) 结构校验   ← workflow_schema.json
  2) 同行不变量: 每个 level.index == 数组下标（同一行渲染/并发的前提）
  3) 节点 id 全局唯一 + agent 引用必须存在（不许幽灵引用）

任何一关失败都抛 ContractError（带可读原因），/run 返回 422 而不是带着
烂数据进调度器。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import jsonschema

PROJECT_ROOT = Path(__file__).resolve().parents[2]          # backend/
SCHEMA_PATH = PROJECT_ROOT.parent / "workflow_schema.json"  # 仓库根 workflow_schema.json
WORKFLOWS_DIR = PROJECT_ROOT.parent / "workflows"           # 按 workflow_id 存放的目录


class ContractError(ValueError):
    pass


@dataclass
class LoadedWorkflow:
    raw: dict[str, Any]                  # 原始 JSON（含 meta/agents/levels/qa）
    workflow_id: str
    path: Path

    @property
    def levels(self) -> list[dict[str, Any]]:
        return self.raw["levels"]

    @property
    def agents(self) -> dict[str, dict[str, Any]]:
        return self.raw.get("agents", {})

    def node_map(self) -> dict[str, dict[str, Any]]:
        """node_id -> node（跨行扁平化，供 executor 查 provider/tools）。"""
        out: dict[str, dict[str, Any]] = {}
        for lvl in self.levels:
            for nd in lvl["nodes"]:
                out[nd["id"]] = nd
        return out


def validate_contract(raw: dict[str, Any]) -> None:
    """三关校验，全过才放行。"""
    # 第 1 关：JSON Schema
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft7Validator(schema)
    errors = sorted(validator.iter_errors(raw), key=lambda e: list(e.path))
    if errors:
        raise ContractError("workflow.json 不符合 workflow_schema.json: "
                            + "; ".join(f"{'/'.join(map(str, e.path))}: {e.message}" for e in errors[:5]))

    # 第 2 关：同行不变量（Strict Constraint #1 的引擎侧背书）
    for i, lvl in enumerate(raw.get("levels", [])):
        if lvl.get("index") != i:
            raise ContractError(f"同行不变量破坏: levels[{i}].index={lvl.get('index')}，必须等于数组下标（同一行 = 同一 index）")

    # 第 3 关：节点 id 唯一 + agent 引用存在
    seen: set[str] = set()
    agents = raw.get("agents", {})
    for lvl in raw.get("levels", []):
        for nd in lvl.get("nodes", []):
            if nd["id"] in seen:
                raise ContractError(f"节点 id 重复: {nd['id']}")
            seen.add(nd["id"])
            if nd.get("kind") in ("agent", "qa", "review", "gate", None):
                ref = nd.get("agent")
                if ref and ref not in agents:
                    raise ContractError(f"节点 {nd['id']} 引用了不存在的 agent '{ref}'（注册表缺失，拒绝幽灵引用）")


def load_workflow(workflow_id: Optional[str] = None, raw: Optional[dict[str, Any]] = None) -> LoadedWorkflow:
    """
    加载策略（kudosflow 式，配置文件是唯一信令）：
      - 传入 raw dict      → 直接校验（/run?inline 或测试用）
      - workflow_id 缺省  → 读仓库根 workflow.json（画布默认保存目标）
      - workflow_id=foo   → 读 workflows/foo.json
    """
    if raw is None:
        if workflow_id:
            path = WORKFLOWS_DIR / f"{workflow_id}.json"
            if not path.exists():
                raise ContractError(f"找不到工作流配置: {path}（画布保存时会自动写入）")
            raw = json.loads(path.read_text(encoding="utf-8"))
        else:
            path = PROJECT_ROOT.parent / "workflow.json"
            if not path.exists():
                raise ContractError(f"默认工作流不存在: {path}")
            raw = json.loads(path.read_text(encoding="utf-8"))
            workflow_id = "default"
    validate_contract(raw)
    return LoadedWorkflow(raw=raw, workflow_id=workflow_id or "default",
                          path=Path(raw.get("meta", {}).get("projectRoot", "./")))
