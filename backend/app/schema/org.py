"""
org.py —— Phase 2b：部门实体化 + 打包模板 + 继承合并 + Org Chart 构建。

任务书对齐：
  任务1（部门实体化与继承）
    departments 支持两种形态（向后兼容）：
      旧:  [{id, title}]  字符串标签列表
      新:  {dev: {head, tools, boundaries, template}}  部门实体字典
    引擎级继承 = 解析 Agent 时，个人 tools 与部门 tools 取【并集】，
    boundaries（mustNot / requiresHumanApproval）取【并集】——只收紧，不放权。
  任务2（打包模板系统）
    内置模板库 = backend/app/templates/<id>.json（受信任源，零网络依赖）。
    部门声明 template: "agile_dev_squad" → 引擎在【加载时解包（Unpack）】：
      · 模板 agents 注入注册表（显式定义优先，绝不覆盖同名）
      · 模板部门标配（head/tools/boundaries）并入部门（显式配置优先）
      · 注入的员工自动归入引用该模板的部门（department 缺省填充）
  任务3（Org Chart）
    build_org_chart() 输出「CEO(Router) → 部门 Head → 部门内 Agent」：
      嵌套树（tree）+ 扁平（flat，带 parent_id）双结构 —— 前端
      React Flow / 树组件两种渲染方案都直接可吃；可选挂 live 资源状态。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Optional

# backend/app/schema/org.py → parents[3] = backend/ → 内置模板库
TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "app" / "templates"

CEO_ID = "ceo"                      # Org Chart 根节点：CEO（Router = 调度引擎本身）
CEO_TITLE = "CEO (Router)"


class TemplateError(ValueError):
    """模板解析/引用错误（load_workflow 会包装成 ContractError → 422）。"""


# --------------------------------------------------------------------------- 模板库
def _read_template_file(tpl_id: str) -> dict[str, Any]:
    """按 id 读内置模板（templates/<id>.json）。结构最小校验：受信任源，仍设防。"""
    p = TEMPLATES_DIR / f"{tpl_id}.json"
    if not p.exists():
        known = sorted(x.stem for x in TEMPLATES_DIR.glob("*.json")) if TEMPLATES_DIR.exists() else []
        raise TemplateError(f"未知模板 '{tpl_id}'（内置库: {known or '空'}）")
    tpl = json.loads(p.read_text(encoding="utf-8"))
    if tpl.get("template_id") != tpl_id:
        raise TemplateError(f"模板 {p.name} 的 template_id='{tpl.get('template_id')}' 与文件名不符")
    agents = tpl.get("agents")
    if not isinstance(agents, list) or not agents:
        raise TemplateError(f"模板 '{tpl_id}' 缺 agents[]（模板必须至少预置一名员工）")
    for a in agents:
        for req in ("id", "role", "provider", "tools", "boundaries"):
            if req not in a:
                raise TemplateError(f"模板 '{tpl_id}' 的 agent '{a.get('id')}' 缺必需字段 '{req}'")
    return tpl


def list_templates() -> list[dict[str, Any]]:
    """GET /templates 数据源：内置模板清单（前端"新建部门"下拉用）。"""
    out = []
    for p in sorted(TEMPLATES_DIR.glob("*.json")):
        tpl = _read_template_file(p.stem)
        out.append({
            "template_id": tpl["template_id"],
            "title": tpl.get("title", ""),
            "description": tpl.get("description", ""),
            "agents": [a["id"] for a in tpl["agents"]],
            "departmentHead": tpl.get("departmentHead"),
            "departmentTools": tpl.get("departmentTools", []),
        })
    return out


# --------------------------------------------------------------------------- 部门归一
def departments_map(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """departments 两种形态 → 统一 {dept_id: 实体dict}（旧数组 = 无继承能力的退化实体）。"""
    d = raw.get("departments")
    if isinstance(d, dict):
        return {k: copy.deepcopy(v) for k, v in d.items()}
    if isinstance(d, list):
        return {x["id"]: {"id": x["id"], "title": x.get("title", x["id"])} for x in d if x.get("id")}
    return {}


# --------------------------------------------------------------------------- 模板解包
def unpack_templates(raw: dict[str, Any]) -> list[str]:
    """加载期解包：部门.template → 注入 agents + 合并部门标配（显式优先）。

    时序约定（validator 定）：JSON Schema 校验跑在解包【之前】（校验的是用户写的
    原始配置）；解包注入的 agent 是引擎受信任源（templates/*.json 最小结构校验过），
    不再过 schema，_source_template 标记因此合法（schema 只约束用户配置，不约束引擎运行态）。

    原地修改 raw（运行态 = 解包后），返回被注入的 agent id 列表（打卡/测试断言用）。
    """
    injected: list[str] = []
    agents = raw.setdefault("agents", {})
    depts_raw = raw.get("departments")
    if not isinstance(depts_raw, dict):
        return injected                     # 旧数组形态：无模板引用能力（向后兼容，原样放行）
    for dept_id, dept in depts_raw.items():
        tpl_id = dept.get("template")
        if not tpl_id:
            continue
        tpl = _read_template_file(tpl_id)
        # ① 员工注入（显式定义优先，绝不覆盖）+ 归入引用部门
        for a in tpl["agents"]:
            aid = a["id"]
            if aid in agents:
                continue                          # 显式定义 wins
            agent = copy.deepcopy(a)
            if not agent.get("department"):
                agent["department"] = dept_id     # 模板员工归入引用部门（横抽池生效）
            agent["_source_template"] = tpl_id    # 溯源标记（引擎运行态字段，校验已过）
            agents[aid] = agent
            injected.append(aid)
        # ② 部门标配并入（显式字段优先；tools 并集；boundaries 合并收紧）
        if not dept.get("head") and tpl.get("departmentHead"):
            dept["head"] = tpl["departmentHead"]
        if tpl.get("departmentTools"):
            merged = list(dict.fromkeys(list(dept.get("tools", [])) + list(tpl["departmentTools"])))
            dept["tools"] = merged
        if tpl.get("departmentBoundaries"):
            _merge_boundaries(dept, tpl["departmentBoundaries"])
    return injected


def _merge_boundaries(target: dict[str, Any], extra: dict[str, Any]) -> None:
    """boundaries 合并 = 各清单并集（去重、target 在前）——只收紧，不放权。"""
    for key in ("mustNot", "requiresHumanApproval"):
        merged = list(dict.fromkeys(list((target.get("boundaries") or {}).get(key, []))
                                    + list((extra or {}).get(key, []))))
        if merged:
            target.setdefault("boundaries", {})[key] = merged


# --------------------------------------------------------------------------- 继承合并
def effective_agent(agent_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    """任务1 引擎级继承：解析一个 Agent = 个人 ∪ 部门（tools 并集 / boundaries 收紧合并）。

    返回全新 dict（不改 raw）；无部门 / 部门无继承字段 = 原样（旧配置零行为变化）。
    """
    ag = copy.deepcopy(raw.get("agents", {}).get(agent_id, {}))
    dept_id = ag.get("department")
    if not dept_id:
        return ag
    dept = departments_map(raw).get(dept_id)
    if not dept:
        return ag
    inherited: dict[str, Any] = {}
    if dept.get("tools"):
        personal = list(ag.get("tools", []))
        union = list(dict.fromkeys(personal + list(dept["tools"])))
        if union != personal:
            ag["tools"] = union
            inherited["tools"] = [t for t in union if t not in personal]
    if dept.get("boundaries"):
        merged_before = copy.deepcopy(ag.get("boundaries") or {})
        _merge_boundaries(ag, dept["boundaries"])
        if (ag.get("boundaries") or {}) != merged_before:
            inherited["boundaries"] = {k: (ag.get("boundaries") or {}).get(k)
                                       for k in ("mustNot", "requiresHumanApproval")
                                       if (ag.get("boundaries") or {}).get(k)}
    if inherited:
        ag["_inherited"] = {"department": dept_id, **inherited}
    return ag


def effective_agents(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """全员继承视图（orchestrator / executor / squad / resource_manager 消费入口）。"""
    return {aid: effective_agent(aid, raw) for aid in raw.get("agents", {})}


# --------------------------------------------------------------------------- Org Chart
def build_org_chart(raw: dict[str, Any], workflow_id: str = "default",
                   states: Optional[dict[str, dict[str, Any]]] = None,
                   states_source: str = "configured") -> dict[str, Any]:
    """任务3：CEO(Router) → 部门 Head → 部门内 Agent。

    states: 可选的 live 资源快照（ResourceManager.snapshot()，agent_id → {state,...}），
            挂进每个 agent 节点（前端"点击 Head 展开看旗下谁在搬砖"的数据源）。
    返回双结构：tree（嵌套）+ flat（parent_id 扁平），前端树/图两种渲染都直接可吃。
    """
    eff = effective_agents(raw)
    depts = departments_map(raw)
    unowned: list[str] = []

    tree: list[dict[str, Any]] = []
    flat: list[dict[str, Any]] = []
    flat.append({"id": CEO_ID, "parent": None, "type": "ceo", "title": CEO_TITLE})

    total_agents = len(eff)
    for did in sorted(depts):
        dept = depts[did]
        head = dept.get("head")
        members = sorted(aid for aid, a in eff.items() if a.get("department") == did)
        agents_out = []
        for aid in members:
            a = eff[aid]
            st = (states or {}).get(aid)
            node: dict[str, Any] = {
                "id": aid, "type": "agent", "parent": did,
                "role": a.get("role", ""), "provider": a.get("provider", "mock"),
                "tools": a.get("tools", []),
                "head": head is not None and aid == head,
                "inherited": a.get("_inherited") or None,       # 继承溯源（前端可展示"部门标配"角标）
                "template": a.get("_source_template") or None,  # 模板注入溯源
            }
            if st is not None:
                node["state"] = st.get("state")
                node["state_source"] = states_source
                if st.get("remaining_sec") is not None:
                    node["resting_sec"] = round(st["remaining_sec"], 1)
            flat.append(node)
            agents_out.append(node)
        dept_node = {
            "id": did, "type": "department", "parent": CEO_ID,
            "title": dept.get("title", did),
            "head": head,
            "template": dept.get("template"),
            "tools": dept.get("tools", []),
            "boundaries": dept.get("boundaries") or None,
            "members": members,
            "agents": agents_out,
        }
        tree.append(dept_node)
        flat.append({"id": did, "parent": CEO_ID, "type": "department",
                     "title": dept_node["title"], "head": head, "member_count": len(members)})

    # 未归属部门的散兵（无 departments 声明的旧配置全员在此）：挂 CEO 直属，Org Chart 不丢人
    owned = {m for d in tree for m in d["members"]}
    unowned = sorted(set(eff) - owned)
    if unowned:
        root_node = {"id": CEO_ID, "type": "ceo", "parent": None, "title": CEO_TITLE,
                     "agents": [{"id": a, "type": "agent", "parent": CEO_ID,
                                  "role": eff[a].get("role", ""), "provider": eff[a].get("provider", "mock")}
                                 for a in unowned]}
        tree.insert(0, root_node)
        for a in unowned:
            flat.append({"id": a, "parent": CEO_ID, "type": "agent", "role": eff[a].get("role", "")})

    return {
        "workflow_id": workflow_id,
        "ceo": {"id": CEO_ID, "title": CEO_TITLE,
                "department_count": len(tree), "agent_count": total_agents},
        "tree": tree,
        "flat": flat,
        "states_source": states_source if states is not None else None,
    }


# --------------------------------------------------------------------------- 组织校验
def validate_org(raw: dict[str, Any]) -> None:
    """Phase 2b 组织语义关（validator 第 4f 关，unpack 之后跑）：
    · 部门 head 引用必须存在（解包后的注册表）
    · head 必须是本部门的员工（department 字段一致，防止 Head 挂在门外）
    · 部门 tools 必须与工具词汇表相容（防模板时代手写错别字进继承链）
    """
    TOOL_VOCAB = {"read_file", "write_file", "patch", "terminal", "compiler",
                   "web_search", "web_extract", "git", "browser"}
    agents = raw.get("agents", {})
    for did, dept in departments_map(raw).items():
        head = dept.get("head")
        if head:
            if head not in agents:
                raise TemplateError(f"部门 '{did}' 的 head='{head}' 不在 agent 注册表（模板解包后仍无此人）")
            if agents[head].get("department") not in (did, None):
                raise TemplateError(f"部门 '{did}' 的 head='{head}' 未归属本部门（department='{agents[head].get('department')}'）")
        bad = [t for t in dept.get("tools", []) if t not in TOOL_VOCAB]
        if bad:
            raise TemplateError(f"部门 '{did}' 的 tools 含未知工具 {bad}（词汇表外，拒绝进继承链）")
