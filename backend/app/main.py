"""
★ Python 调度引擎入口（Task 1）。

    uvicorn app.main:app --port 8790        # 在 backend/ 目录下启动

暴露：
    GET  /health          → 引擎 + 契约状态
    POST /run             → {workflow_id?, inline?}  读取本地 JSON 并拉起 Orchestrator（异步执行，立即返回 run_id）
    GET  /runs            → 历史 run 状态（内存版，Phase 2 落盘）
    WS   /ws/run          → 事件总线实时流（Task 3）

kudosflow 式铁律：引擎【只读】 workflow.json / workflows/<id>.json ——
前端画布保存时覆盖文件，引擎每次 /run 都重新加载 + 三关校验，绝不维护自己的配置副本。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from .engine.events import EventBus
from .engine.orchestrator import Orchestrator
from .persistence.progress import ProgressLog
from .schema.validator import ContractError, load_workflow
from .schema.org import build_org_chart, list_templates
from .ws.stream import Hub, stream_endpoint

# main.py 位于 backend/app/ 下：parents[2] = 仓库根 (H:/project/company)
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ROOT = REPO_ROOT                        # progress.md / checkpoint 落仓库根

app = FastAPI(title="the-company engine", version="0.4.0")

# 全局单例：一个 Hub（进程级广播中枢）+ run 记录
hub = Hub(bus=EventBus(run_id="engine"))   # engine bus 只挂 hub；每个 run 有独立 bus
RUNS: dict[str, dict[str, Any]] = {}


class RunRequest(BaseModel):
    """POST /run 请求体。"""
    workflow_id: Optional[str] = None      # 读 workflows/<id>.json；缺省读仓库根 workflow.json
    inline: Optional[dict[str, Any]] = None  # 测试通道：直接传 raw dict（跳过文件）
    project_root: Optional[str] = None      # progress.md 落盘位置（默认仓库根）


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"engine": "ok", "hub_clients": len(hub._clients), "runs": len(RUNS)}


@app.post("/run")
async def run(req: RunRequest) -> dict[str, Any]:
    """读取本地 JSON + 三关校验 + 拉起 Orchestrator。校验失败 = 422（带可读原因）。"""
    if req.inline is not None:
        from .schema.validator import validate_contract
        raw = req.inline
    else:
        raw = None

    try:
        wf = load_workflow(workflow_id=req.workflow_id, raw=raw)
    except (ContractError, FileNotFoundError) as e:
        raise HTTPException(status_code=422, detail=str(e))

    root = req.project_root or str(DEFAULT_ROOT)
    bus = EventBus()
    progress = ProgressLog(root)
    # ★ V2：资源管理器（provider 限流冷却门 + 状态机 + Tick 心跳随 orchestrator.run() 起停）
    from .engine.resource_manager import ResourceManager
    rm = ResourceManager(wf, bus)
    # ★ Phase 3a：Git 分支隔离 + PR 门禁（仅 workflow 配了 git_policy 时启用）
    from .engine.git_policy import GitPolicy
    gp = GitPolicy(wf, bus) if wf.raw.get("git_policy") else None
    orch = Orchestrator(wf, bus, progress, checkpoint_root=root, rm=rm, git_policy=gp)

    # run 事件接入 Hub：本 run 的所有事件扇出给每个 WS 客户端（按 ?run_id= 过滤）
    hub.link(bus)

    run_id = bus.run_id
    RUNS[run_id] = {"workflow": wf.workflow_id, "status": "running", "result": None,
                    "rm": rm, "bus": bus}

    async def _drive() -> None:
        result = await orch.run()
        RUNS[run_id]["status"] = "done" if result["success"] else "failed"
        RUNS[run_id]["result"] = result
        # run 结束：快照终态资源状态（供 /resources 历史查询）
        RUNS[run_id]["resources"] = rm.snapshot()

    import asyncio
    asyncio.create_task(_drive())
    return {"run_id": run_id, "workflow": wf.workflow_id,
            "levels": len(wf.levels), "status": "started"}


# 进程级：Hub 接到 engine bus（health/计数用）；每个 /run 再 link 各自 run bus
hub.attach()


# WebSocket：Task 3 实时日志流（?run_id=xxx 可过滤到单个 run）
app.add_api_websocket_route("/ws/run", stream_endpoint(hub))


# ---------------------------------------------------------------- 历史 run 查询
@app.get("/runs")
async def runs() -> list[dict[str, Any]]:
    # 只导出可序列化字段（rm/bus = 活对象，/resources 端点内部消费，不进 JSON）
    return [{k: v for k, v in RUNS[k].items() if k not in ("rm", "bus")} | {"run_id": k}
            for k in RUNS]


# ---------------------------------------------------------------- Phase 2b 组织 API
@app.get("/org_chart")
async def org_chart(workflow_id: Optional[str] = Query(default=None, description="读 workflows/<id>.json；缺省=仓库根 workflow.json"),
                   live: bool = Query(default=False, description="挂最近一次 run 的实时资源状态（谁在搬砖/谁在休息）")) -> dict[str, Any]:
    """任务3：CEO(Router) → 部门 Head → 部门内 Agent 结构化组织树。
    双结构返回：tree（嵌套）+ flat（带 parent，React Flow 直接 nodes/edges）。"""
    try:
        wf = load_workflow(workflow_id=workflow_id)
    except (ContractError, FileNotFoundError) as e:
        raise HTTPException(status_code=422, detail=str(e))

    states: Optional[dict[str, Any]] = None
    states_source = "configured"
    if live:
        # 最近一个含 rm 的 run（未完成=实时快照，已完成=终态快照）→ "点击 Head 展开看旗下谁在搬砖"
        for run_id in reversed(list(RUNS)):
            entry = RUNS[run_id]
            rm = entry.get("rm")
            if rm is None:
                continue
            states = entry.get("resources") or rm.snapshot()
            states_source = "live" if entry["status"] == "running" else "last_run"
            break

    return build_org_chart(wf.raw, workflow_id=wf.workflow_id,
                           states=states, states_source=states_source)


@app.get("/templates")
async def templates() -> dict[str, Any]:
    """Phase 2b：内置部门模板库清单（前端"新建部门"下拉选模板用；零网络依赖）。"""
    return {"templates": list_templates()}


@app.get("/resources/{run_id}")
async def resources(run_id: str) -> dict[str, Any]:
    """V2 站会大盘数据源：某 run 结束时的员工资源快照（状态/余量/恢复时刻/部门）。
    未结束的 run 返回实时快照；未知 run_id = 404。"""
    entry = RUNS.get(run_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"未知 run_id {run_id}")
    rm: Optional["ResourceManager"] = entry.get("rm")
    if rm is None:
        raise HTTPException(status_code=409, detail="该 run 无资源管理器（Phase1 旧 run）")
    snap = entry.get("resources") or rm.snapshot()
    return {"run_id": run_id, "status": entry["status"], "states": snap}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8790, reload=False)
