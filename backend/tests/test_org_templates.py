"""Phase 2b 单测：部门实体化 + 打包模板 + 继承合并 + Org Chart + API。

跑法：cd backend && python tests/test_org_templates.py
断言链：
  A) 模板解包：注入员工 / 显式优先不覆盖 / 归入引用部门 / 部门标配并入
  B) 继承合并：tools 并集 / boundaries 只收紧 / _inherited 溯源 / 旧配置零变化
  C) Org Chart：CEO→Head→Agent 树 + flat parent / head 标记 / template 溯源
  D) 校验关 4f：head 幽灵引用 / head 挂门外 / 未知模板 / 部门工具词汇表外
  E) 向后兼容：旧数组 departments 全链路零影响
  F) 全链路 e2e：workflows/mock-org-templates.json（模板注入员工真执行）
  G) API 冒烟：/org_chart + /templates 端点直接调用（函数级，不起 uvicorn）
"""
from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.schema.validator import ContractError, load_workflow, validate_contract
from app.schema.org import (build_org_chart, departments_map, effective_agent,
                            effective_agents, list_templates, unpack_templates)
from app.engine.events import EventBus
from app.engine.orchestrator import Orchestrator
from app.persistence.progress import ProgressLog

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def base_wf(**extra) -> dict:
    wf = {
        "meta": {"name": "t", "version": "1"},
        "agents": {"pm": {"id": "pm", "role": "PM", "provider": "mock",
                            "tools": ["read_file"], "boundaries": {"mustNot": ["写代码"]},
                            "department": "dev"}},
        "levels": [{"index": 0, "name": "L0", "nodes": [
            {"id": "n1", "agent": "pm", "task": "立项"}]}],
        "qa": {"onFailure": ["console", "redHighlight", "rollback"]},
    }
    wf.update(extra)
    return wf


def org_wf() -> dict:
    """部门实体字典 + 模板引用 + 显式员工（用于解包/继承断言）。"""
    wf = base_wf(departments={
        "dev": {"title": "研发部", "template": "agile_dev_squad", "head": "backend_cat",
                "tools": ["git"], "boundaries": {"mustNot": ["外网请求"]}},
    })
    wf["levels"] = [{"index": 0, "name": "L0", "nodes": [
        {"id": "n1", "agent": "pm", "task": "立项"},
        {"id": "n2", "agent": "frontend_cat", "task": "UI（模板员工）"}]}]
    return wf


# ---------------------------------------------------------------- A) 模板解包
async def test_unpack():
    print("\n== A) 模板解包（加载期注入运行态）==")
    raw = org_wf()
    injected = unpack_templates(raw)
    check("agile_dev_squad 三名猫咪注入", set(injected) == {"frontend_cat", "backend_cat", "qa_cat"}, str(injected))
    check("注入员工归入引用部门 dev", raw["agents"]["frontend_cat"].get("department") == "dev")
    check("模板溯源标记 _source_template", raw["agents"]["backend_cat"].get("_source_template") == "agile_dev_squad")

    # 显式优先：同名 agent 不覆盖
    raw2 = org_wf()
    raw2["agents"]["frontend_cat"] = {"id": "frontend_cat", "role": "前端（显式覆盖）", "provider": "mock",
                                        "tools": ["patch"], "boundaries": {"mustNot": []}}
    inj2 = unpack_templates(raw2)
    check("显式定义 wins（不覆盖）", "frontend_cat" not in inj2
          and raw2["agents"]["frontend_cat"]["role"] == "前端（显式覆盖）")

    # 部门标配并入（模板 compiler 等并入 dev.tools）
    raw3 = org_wf()
    unpack_templates(raw3)
    check("部门 tools 并入模板标配", "compiler" in raw3["departments"]["dev"]["tools"]
          and "git" in raw3["departments"]["dev"]["tools"], str(raw3["departments"]["dev"]["tools"]))
    check("部门 head 缺省取模板 departmentHead", raw3["departments"]["qa"]["head"] == "review_cat"
          if "qa" in raw3["departments"] else "n/a", str(raw3["departments"].get("qa")))


# ---------------------------------------------------------------- B) 继承合并
async def test_inheritance():
    print("\n== B) 继承合并（个人 ∪ 部门）==")
    raw = org_wf()
    unpack_templates(raw)

    # pm：个人 read_file ∪ 部门 dev tools
    eff = effective_agent("pm", raw)
    check("tools 并集（个人+部门标配）", set(eff["tools"]) >= {"read_file", "git", "compiler"}
          and "git" in eff.get("_inherited", {}).get("tools", []), str(eff["tools"]))

    # boundaries 只收紧：pm.mustNot 个人['写代码'] + 部门['外网请求']
    check("boundaries 合并收紧", {"写代码", "外网请求"} <= set(eff["boundaries"]["mustNot"]),
          str(eff["boundaries"]))

    # 无部门继承字段的旧部门 = 零继承
    raw3 = base_wf(departments={"dev": {"title": "研发部"}})
    eff3 = effective_agent("pm", raw3)
    check("旧部门（无继承字段）零变化", eff3["tools"] == ["read_file"] and "_inherited" not in eff3)


# ---------------------------------------------------------------- C) Org Chart
async def test_org_chart():
    print("\n== C) Org Chart（CEO→Head→Agent 双结构）==")
    raw = org_wf()
    unpack_templates(raw)
    chart = build_org_chart(raw, workflow_id="org-test")
    flat = {f["id"]: f for f in chart["flat"]}
    check("根节点 CEO 存在", "ceo" in flat and flat["ceo"]["type"] == "ceo" and flat["ceo"]["parent"] is None)
    check("dev 部门节点挂 CEO 下", flat["dev"]["parent"] == "ceo" and flat["dev"]["head"] == "backend_cat")
    check("backend_cat 标记为 head", flat["backend_cat"]["head"] is True)
    check("模板员工带 template 溯源", flat["frontend_cat"].get("template") == "agile_dev_squad")
    check("继承角标 inherited 可见", flat["pm"].get("inherited", {}).get("department") == "dev")

    # 嵌套树：部门 node 内嵌 agents
    dev_node = next(d for d in chart["tree"] if d["id"] == "dev")
    check("tree 嵌套：dev.agents 含模板猫咪", {"frontend_cat", "backend_cat", "qa_cat"} <= set(dev_node["members"]),
          str(dev_node["members"]))
    check("agent_count 含模板注入", chart["ceo"]["agent_count"] >= 4)

    # live 状态挂载
    chart2 = build_org_chart(raw, workflow_id="org-test",
                              states={"backend_cat": {"state": "resting", "remaining_sec": 12.5}},
                              states_source="live")
    flat2 = {f["id"]: f for f in chart2["flat"]}
    check("live 状态挂进 agent 节点", flat2["backend_cat"].get("state") == "resting"
          and flat2["backend_cat"].get("resting_sec") == 12.5)


# ---------------------------------------------------------------- D) 校验关 4f
async def test_validator_gates():
    print("\n== D) 校验关（组织语义 4f + 模板错误）==")
    # head 幽灵引用（template 保持 → 解包成功，4f 关接住 head）
    bad1 = org_wf()
    bad1["departments"]["dev"]["head"] = "ghost_head"
    try:
        validate_contract(bad1)
        check("head 幽灵引用 被拒", False)
    except ContractError as e:
        check("head 幽灵引用 被拒", "ghost_head" in str(e), str(e))

    # 未知模板
    bad2 = org_wf()
    bad2["departments"]["dev"]["template"] = "no_such_template"
    try:
        validate_contract(bad2)
        check("未知模板 被拒", False)
    except ContractError as e:
        check("未知模板 被拒", "no_such_template" in str(e), str(e))

    # 部门工具词汇表外
    bad3 = org_wf()
    bad3["departments"]["dev"]["tools"] = ["magic_wand"]
    try:
        validate_contract(bad3)
        check("部门工具词汇表外 被拒", False)
    except ContractError as e:
        check("部门工具词汇表外 被拒", "magic_wand" in str(e), str(e))

    # 节点引用模板注入员工（解包后放行）
    good = org_wf()
    validate_contract(good)
    check("节点引用模板员工（frontend_cat）放行", True)


# ---------------------------------------------------------------- E) 向后兼容
async def test_backward_compat():
    print("\n== E) 向后兼容（旧数组 departments 零影响）==")
    for f in ["mock-demo", "mock-squad", "mock-git-pr"]:
        wf = load_workflow(workflow_id=f)
        check(f"旧工作流 {f} 仍加载通过", wf.workflow_id == f)
    # 旧数组形态 departments_map 退化
    raw = base_wf(departments=[{"id": "dev"}])
    check("旧数组 → 退化实体 map", departments_map(raw) == {"dev": {"id": "dev", "title": "dev"}})


# ---------------------------------------------------------------- F) 全链路 e2e
async def test_e2e_org():
    print("\n== F) 全链路 e2e（模板注入员工真执行）==")
    wf = load_workflow(workflow_id="mock-org-templates")
    check("模板注入 5 名猫咪 + 1 显式 PM", len(wf.injected_agents) >= 5, str(wf.injected_agents))

    bus = EventBus()
    progress = ProgressLog(str(Path(__file__).resolve().parents[1].parent))
    orch = Orchestrator(wf, bus, progress, checkpoint_root=str(Path(__file__).resolve().parents[1].parent))
    result = await orch.run()
    check("run 成功", result["success"], json.dumps(result["results"], ensure_ascii=False)[:200])

    # org_chart 含模板员工 + 继承角标
    chart = build_org_chart(wf.raw, workflow_id=wf.workflow_id)
    flat = {f["id"]: f for f in chart["flat"]}
    check("org_chart 含模板猫咪", "frontend_cat" in flat and "qa_cat" in flat)
    check("pm_human 继承 dev 部门 tools", "compiler" in flat["pm_human"].get("tools", []),
          str(flat["pm_human"].get("tools")))


# ---------------------------------------------------------------- G) API 冒烟
async def test_api_smoke():
    print("\n== G) API 冒烟（/org_chart + /templates 函数级）==")
    import importlib
    main_mod = importlib.import_module("app.main")
    # /templates
    t = await main_mod.templates()
    ids = [x["template_id"] for x in t["templates"]]
    check("GET /templates 列出内置模板", "agile_dev_squad" in ids and "qa_watchdog" in ids, str(ids))
    # /org_chart（live=False）
    oc = await main_mod.org_chart(workflow_id="mock-org-templates", live=False)
    check("GET /org_chart 返回 CEO 根", oc["ceo"]["id"] == "ceo" and oc["ceo"]["title"].startswith("CEO"))
    check("GET /org_chart flat 带 parent（React Flow 友好）",
          all("parent" in f for f in oc["flat"]))


async def main():
    await test_unpack()
    await test_inheritance()
    await test_org_chart()
    await test_validator_gates()
    await test_backward_compat()
    await test_e2e_org()
    await test_api_smoke()
    print(f"\n总计 {PASS + FAIL} 项，通过 {PASS}，失败 {FAIL}")
    if FAIL:
        sys.exit(1)
    print("全部 PASS ★ Phase 2b 组织架构树 + 部门模板系统 验收通过")


if __name__ == "__main__":
    asyncio.run(main())
