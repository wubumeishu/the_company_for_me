"""
数据契约加载 + 校验 + 同行不变量 + V2 语义校验。

这是整个系统的"生死线"：前端画布保存下来的 workflow.json 进引擎前，
必须在这里过四关——
  1) JSON Schema (draft-07) 结构校验   ← workflow_schema.json
  2) 同行不变量: 每个 level.index == 数组下标（同一行渲染/并发的前提）
  3) 节点 id 全局唯一 + agent 引用必须存在（不许幽灵引用）
  4) V2 语义校验（向后兼容：字段缺省即跳过）
       - agent.department 必须存在于 departments 注册表（声明了部门就必须有注册表）
       - squads[].members[].from / agent 引用必须解析得到（部门有人 / agent 存在）
       - kind=squad 的节点必须带 squad 字段且 squads 里有定义
       - providers[].rate.windows 若声明，limit/periodSec 必须为正
任何一关失败都抛 ContractError（带可读原因），/run 返回 422 而不是带着
烂数据进调度器。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import jsonschema

from .org import (TemplateError, build_org_chart, departments_map,
                  effective_agents, list_templates, unpack_templates,
                  validate_org)

PROJECT_ROOT = Path(__file__).resolve().parents[2]          # backend/
SCHEMA_PATH = PROJECT_ROOT.parent / "workflow_schema.json"  # 仓库根 workflow_schema.json
WORKFLOWS_DIR = PROJECT_ROOT.parent / "workflows"           # 按 workflow_id 存放的目录


class ContractError(ValueError):
    pass


@dataclass
class LoadedWorkflow:
    raw: dict[str, Any]                  # 运行态（= 模板解包后的注册表，引擎消费这个）
    workflow_id: str
    path: Path
    injected_agents: list[str] = field(default_factory=list)   # Phase 2b：模板注入的员工（审计/事件用）
    _effective: Optional[dict[str, dict[str, Any]]] = field(default=None, init=False, repr=False)  # 继承视图缓存

    @property
    def levels(self) -> list[dict[str, Any]]:
        return self.raw["levels"]

    @property
    def agents(self) -> dict[str, dict[str, Any]]:
        return self.raw.get("agents", {})

    @property
    def departments(self) -> list[dict[str, Any]]:
        """旧形态（数组）原样返回；新形态（实体字典）投影成同构数组 —— 消费方零改动。"""
        d = self.raw.get("departments")
        if isinstance(d, dict):
            return [dict(v, id=k) for k, v in d.items()]
        return d or []

    def departments_map(self) -> dict[str, dict[str, Any]]:
        """Phase 2b 部门实体视图：{dept_id: 实体}（旧数组形态 = 退化实体 {id,title}）。"""
        return departments_map(self.raw)

    @property
    def effective_agents(self) -> dict[str, dict[str, Any]]:
        """任务1 继承视图：个人 ∪ 部门（tools 并集 / boundaries 合并收紧）。
        orchestrator / executor / squad / resource_manager 一律消费此视图。
        raw 在 load 后已冻结（解包在 validate_contract 内完成）→ 计算一次缓存。"""
        if self._effective is None:
            self._effective = effective_agents(self.raw)
        return self._effective

    def resolved_agent(self, agent_id: str) -> dict[str, Any]:
        """按 id 取继承后的员工视图（executor 构造入口；未知 id = 空 dict，保持旧语义）。"""
        return self.effective_agents.get(agent_id, {})

    @property
    def squads(self) -> dict[str, Any]:
        return self.raw.get("squads", {})

    def dept_ids(self) -> set[str]:
        return {d.get("id") for d in self.departments if d.get("id")}

    def node_map(self) -> dict[str, dict[str, Any]]:
        """node_id -> node（跨行扁平化，供 executor 查 provider/tools）。"""
        out: dict[str, dict[str, Any]] = {}
        for lvl in self.levels:
            for nd in lvl["nodes"]:
                out[nd["id"]] = nd
        return out


def validate_contract(raw: dict[str, Any]) -> list[str]:
    """五关校验，全过才放行。返回模板解包注入的 agent id 列表（审计/事件用）。

    时序铁律（Phase 2b 定）：
      第 1 关 JSON Schema 校验的是【用户写的原始配置】（additionalProperties:false 全效）；
      之后才 unpack_templates 解包（引擎受信任源，注入 agent 带 _source_template 运行态字段，
      不再过 schema）；第 3 关的 agent 引用检查跑在解包【之后】——
      节点可以直接引用模板注入的员工（如 frontend_cat），无需在配置里重复定义。
    """
    # 第 1 关：JSON Schema（用户配置契约）
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft7Validator(schema)
    errors = sorted(validator.iter_errors(raw), key=lambda e: list(e.path))
    if errors:
        raise ContractError("workflow.json 不符合 workflow_schema.json: "
                            + "; ".join(f"{'/'.join(map(str, e.path))}: {e.message}" for e in errors[:5]))

    # ★ Phase 2b：模板解包（部门.template → 注入员工 + 并入部门标配；旧数组形态原样放行）
    try:
        injected = unpack_templates(raw)
    except TemplateError as e:
        raise ContractError(f"模板解包失败: {e}")

    # 第 2 关：同行不变量（Strict Constraint #1 的引擎侧背书）
    for i, lvl in enumerate(raw.get("levels", [])):
        if lvl.get("index") != i:
            raise ContractError(f"同行不变量破坏: levels[{i}].index={lvl.get('index')}，必须等于数组下标（同一行 = 同一 index）")

    # 第 3 关：节点 id 唯一 + agent 引用存在（解包后：模板员工也算注册表成员）
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

    # 第 4 关：V2 语义校验（字段缺省即跳过 → 旧配置零影响）
    _validate_v2(raw, agents)

    # 第 4f 关：Phase 2b 组织语义关（head 引用 / 部门 tools 词汇 / 模板解包后的引用完整性）
    try:
        validate_org(raw)
    except TemplateError as e:
        raise ContractError(str(e))
    return injected


def _validate_v2(raw: dict[str, Any], agents: dict[str, dict[str, Any]]) -> None:
    """V2 部门/小队/限流引用一致性（防御性：宁可 422 也不带烂数据进调度器）。"""
    # ★ Phase 2b：departments 两种形态（旧数组 / 实体字典）统一走 departments_map
    dept_ids = set(departments_map(raw).keys())

    # 4a. agent.department 声明了就必须存在于 departments 注册表
    for aid, ag in agents.items():
        dept = ag.get("department")
        if dept is not None and dept not in dept_ids:
            raise ContractError(f"agent '{aid}' 声明 department='{dept}'，但 departments 注册表无此部门（V2 部门引用失效）")

    # 4b. squads 成员引用必须可解析（from 是部门且有员，或 agent 显式存在）
    for sid, sq in raw.get("squads", {}).items():
        for m in sq.get("members", []):
            explicit = m.get("agent")
            if explicit:
                if explicit not in agents:
                    raise ContractError(f"squad '{sid}' 成员引用不存在的 agent '{explicit}'")
            else:
                frm = m.get("from")
                if frm in agents:
                    continue                      # from 直接给了 agent id
                # from 是部门 id：该部门下必须至少有一名 agent 可抽
                pool = [a for a, ag in agents.items() if ag.get("department") == frm]
                if not pool:
                    raise ContractError(f"squad '{sid}' 从部门 '{frm}' 抽调，但该部门无可用 agent（横向抽调落空）")

    # 4c. kind=squad 节点必须带 squad 字段且 squads 有定义
    for nd in _all_nodes(raw):
        if nd.get("kind") == "squad":
            sid = nd.get("squad")
            if not sid:
                raise ContractError(f"节点 '{nd['id']}' kind=squad 但缺 squad 字段（未指定敏捷小队）")
            if sid not in raw.get("squads", {}):
                raise ContractError(f"节点 '{nd['id']}' 引用未定义的 squad '{sid}'（squads 注册表缺失）")

    # 4d. providers.rate.windows 若声明，参数必须合法（limit/periodSec 正整数）
    for p in raw.get("providers", []):
        for w in (p.get("rate") or {}).get("windows", []):
            if int(w.get("limit", 0)) < 1 or int(w.get("periodSec", 0)) < 1:
                raise ContractError(f"provider '{p.get('id')}' 的 rate.windows 参数非法: {w}（limit/periodSec 必须 ≥1）")

    # 4e. Phase 3a：pr_gate 节点必须在顶层配 git_policy（没有配置权威层 = 门禁形同虚设）
    has_pr_gate = any(nd.get("kind") == "pr_gate" for nd in _all_nodes(raw))
    if has_pr_gate and "git_policy" not in raw:
        raise ContractError("存在 kind=pr_gate 节点但顶层未配置 git_policy（PR 门禁缺少引擎权威层配置）")


def _all_nodes(raw: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for lvl in raw.get("levels", []):
        out.extend(lvl.get("nodes", []))
    return out


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
    injected = validate_contract(raw)
    return LoadedWorkflow(raw=raw, workflow_id=workflow_id or "default",
                          path=Path(raw.get("meta", {}).get("projectRoot", "./")),
                          injected_agents=injected)
