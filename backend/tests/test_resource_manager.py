"""Phase 2 V2 单测：ResourceManager（冷却恢复时间精确计算 + Tick 调度）+ Squad（抽调/解散）。

全部用"可注入时钟"冻结时间精确断言，不真等 60s/5h：
    run:  cd backend && python tests/test_resource_manager.py
重点（任务书点名）：
  1) 冷却恢复时间计算：触限后 resume_at = 窗口最早重置点 + cooldown，逐值断言
  2) Tick 调度：到期前 tick 不唤醒 / 到期那拍唤醒 / 多窗口（分钟+小时）取最保守恢复点
  3) Squad：部门解析、会议室共享 state、QA 门禁、自动解散归建
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.engine.events import EventBus
from app.engine.orchestrator import Orchestrator
from app.engine.resource_manager import (
    ResourceManager, RateLedger, TickDriver, TokenEstimator,
    RESOURCE_ACQUIRED, RESOURCE_RESTING, STATE_ACTIVE, STATE_RESTING, STATE_SQUAD,
)
from app.engine.squad import SquadExecutor, SquadRoom
from app.persistence.progress import ProgressLog
from app.schema.validator import LoadedWorkflow, load_workflow

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, cond, detail))
    print(f"  {PASS if cond else FAIL}  {name}  {detail}")


def make_wf(providers=None, agents=None, tick=0.5, departments=None, squads=None):
    """构造一个最小 workflow raw（绕过文件 IO，直接建 LoadedWorkflow）。"""
    raw = {
        "meta": {"name": "t", "version": "0.0", "tickIntervalSec": tick},
        "agents": agents or {"a1": {"id": "a1", "role": "dev", "provider": "claude",
                                     "tools": [], "boundaries": {}}},
        "levels": [{"index": 0, "name": "L0", "nodes": [
            {"id": "n0", "agent": "a1", "task": "x"}]}],
        "qa": {"onFailure": ["console"]},
    }
    if providers: raw["providers"] = providers
    if departments: raw["departments"] = departments
    if squads: raw["squads"] = squads
    return LoadedWorkflow(raw=raw, workflow_id="t", path=Path("."))


def frozen_clock(t0: float = 1000.0):
    box = {"t": t0}
    return (lambda: box["t"]), box


# ===================================================================== 任务2.1 冷却恢复时间
def test_cooldown_time():
    print("\n== 冷却恢复时间计算（RateLedger）==")
    from app.engine.resource_manager import WindowSpec
    led = RateLedger([WindowSpec(limit=3, period_sec=60)])   # 3次/60s
    now = 1000.0

    for _ in range(3):
        ok, _ = led.consume(1, now=now)
        check(f"第3次内放行", ok)
    ok, resume = led.consume(1, now=now)
    check("第4次触限拒绝", not ok)
    check("恢复时间 = 最旧戳+周期 (1000+60=1060)", abs(resume - 1060.0) < 1e-6, f"resume={resume}")
    # 窗口未清，remaining=0
    check("触限后余量=0", led.remaining(now)[0]["remaining"] == 0)

    # 多窗口（分钟级 + 小时级）取最保守恢复点
    led2 = RateLedger([WindowSpec(limit=2, period_sec=60),
                       WindowSpec(limit=100, period_sec=18000, cooldown_sec=30)])
    now2 = 2000.0
    for _ in range(2):
        led2.consume(1, now=now2)
    ok2, resume2 = led2.consume(1, now=now2)
    check("双窗口第3次触限", not ok2)
    # 分钟窗口满→最旧戳2000+60=2060；小时窗口未满→余量足，恢复点取分钟窗口
    check("恢复点=分钟窗口重置(2060)", abs(resume2 - 2060.0) < 1e-6, f"resume={resume2}")

    # 小时窗口带 cooldown：单独打满
    led3 = RateLedger([WindowSpec(limit=1, period_sec=18000, cooldown_sec=30)])
    now3 = 3000.0
    led3.consume(1, now=now3)
    ok3, resume3 = led3.consume(1, now=now3)
    check("小时窗口触限拒绝", not ok3)
    check("恢复点=最早重置+cooldown (3000+18000+30=21030)", abs(resume3 - 21030.0) < 1e-6, f"resume={resume3}")


# ===================================================================== 任务2.2 Tick 调度
async def test_tick():
    print("\n== Tick 调度（注入时钟冻结时间，不真等）==")
    providers = [{"id": "claude", "type": "anthropic",
                  "rate": {"windows": [{"limit": 2, "periodSec": 60}]}}]
    wf = make_wf(providers=providers)
    get_clock, box = frozen_clock(1000.0)
    bus = EventBus()
    rm = ResourceManager(wf, bus, clock=get_clock)

    # 连扣 2 次放行
    v1, _ = await rm.acquire("a1", tokens=1)
    v2, _ = await rm.acquire("a1", tokens=1)
    check("前两笔 ACQUIRED", v1 == RESOURCE_ACQUIRED and v2 == RESOURCE_ACQUIRED)

    # 第 3 次触限 → RESTING + 精确恢复时刻
    v3, resume3 = await rm.acquire("a1", tokens=1)
    check("第三笔 RESTING（触限拦截下一任务）", v3 == RESOURCE_RESTING)
    check("RESTING 恢复时刻精确 = 1060", abs(resume3 - 1060.0) < 1e-6, f"resume_at={resume3}")
    check("状态=RESTING", rm.states["a1"].state == STATE_RESTING)

    # 到期前 tick：不唤醒
    box["t"] = 1050.0
    woke = await rm.tick()
    check("到期前(t=1050)tick 不唤醒", woke == [] and rm.states["a1"].state == STATE_RESTING)

    # 到期 tick：唤醒
    box["t"] = 1060.0
    woke = await rm.tick()
    check("到期(t=1060)tick 唤醒", "a1" in woke and rm.states["a1"].state == STATE_ACTIVE)

    # 恢复后可再次扣减
    v4, _ = await rm.acquire("a1", tokens=1)
    check("恢复后 ACQUIRED", v4 == RESOURCE_ACQUIRED)


# ===================================================================== 任务2.3 TokenEstimator
def test_estimator():
    print("\n== TokenEstimator（tokenBudget 缺省启发式 / 显式优先）==")
    node_explicit = {"task": "x", "tokenBudget": {"estimate": 42000}}
    check("显式 estimate 优先=42000", TokenEstimator.estimate(node_explicit, {}) == 42000)

    node = {"task": "实现并发调度模块", "inputs": []}
    est = TokenEstimator.estimate(node, {"n0": 4000})   # 上游 4000 字节 → /4
    check("缺省=task*0.3 + 上游字节/4 + outputHint(256)", est >= 256 + 4000 // 4, f"est={est}")


# ===================================================================== 任务3 Squad
def test_squad():
    print("\n== 敏捷小队（部门解析 + 会议室 + QA 门禁 + 自动解散）==")
    departments = [{"id": "pm"}, {"id": "dev"}, {"id": "qa"}]
    agents = {
        "pm1":  {"id": "pm1", "role": "PM", "provider": "mock", "tools": [], "boundaries": {}, "department": "pm"},
        "dev1": {"id": "dev1", "role": "Dev", "provider": "mock", "tools": [], "boundaries": {}, "department": "dev"},
        "dev2": {"id": "dev2", "role": "Dev", "provider": "mock", "tools": [], "boundaries": {}, "department": "dev"},
        "qa1":  {"id": "qa1", "role": "QA", "provider": "mock", "tools": [], "boundaries": {}, "department": "qa"},
    }
    squads = {"big_feat": {"members": [
        {"from": "pm", "as": "pm"},
        {"from": "dev", "as": "dev"},
        {"from": "dev", "as": "dev", "agent": "dev2"},   # 显式指定 dev2
        {"from": "qa", "as": "qa"},
    ], "dissolveOn": "squad_qa_pass"}}
    wf = make_wf(agents=agents, departments=departments, squads=squads)
    bus = EventBus()
    rm = ResourceManager(wf, bus, clock=frozen_clock()[0])
    ex = SquadExecutor(wf, bus, ProgressLog(str(ROOT.parent)), rm)

    # 部门解析：dev 部门抽 2 人（按 id 排序 dev1 + 显式 dev2）
    members = ex.resolve_members(squads["big_feat"])
    ids = [m.agent_id for m in members]
    check("部门解析 PM/Dev×2/QA 共4人", len(ids) == 4, f"{ids}")
    check("显式 agent 指定生效(dev2)", "dev2" in ids)
    check("部门抽取确定性(dev1 是 dev 部门按序第一)", "dev1" in ids)

    # 会议室共享 state + QA 门禁
    room = SquadRoom("big_feat", "大型特性")
    room.join("pm1", "pm"); room.join("dev1", "dev"); room.join("dev2", "dev"); room.join("qa1", "qa")
    room.contribute("pm", "pm1", "plan", "拆3块")
    room.contribute("dev", "dev1", "code", "调度")
    room.contribute("dev", "dev2", "code", "画布")
    room.contribute("qa", "qa1", "checklist", "清单")
    ok, missing = room.qa_check()
    check("全员有产出 → QA 放行", ok, f"missing={missing}")

    # 缺席一人（dev2 没产出）→ QA 打回
    room2 = SquadRoom("big_feat", "x")
    room2.join("dev1", "dev"); room2.join("qa1", "qa")
    room2.contribute("qa", "qa1", "checklist", "清单")   # dev1 缺产出
    ok2, missing2 = room2.qa_check()
    check("缺产出 → QA 打回", not ok2 and "dev1" in missing2, f"missing={missing2}")


# ===================================================================== 部门引用失效（V2 校验）
def test_dept_reject():
    print("\n== V2 校验（部门引用失效被拒）==")
    from app.schema.validator import _validate_v2, ContractError
    bad = {"agents": {"x": {"department": "ghost"}}, "levels": []}
    try:
        _validate_v2(bad, bad["agents"])
        check("agent 声明不存在部门 被拒", False)
    except ContractError as e:
        check("agent 声明不存在部门 被拒", "departments" in str(e))

    # squad 抽一个没人的部门 → 拒
    bad2 = {"departments": [{"id": "dev"}], "agents": {},
            "squads": {"s": {"members": [{"from": "dev", "as": "dev"}]}}, "levels": []}
    try:
        _validate_v2(bad2, bad2["agents"])
        check("squad 抽空部门 被拒", False)
    except ContractError as e:
        check("squad 抽空部门 被拒", "无可用 agent" in str(e))


# ===================================================================== 任务2.4 冷却挂起→tick 唤醒→重跑（编排集成）
async def test_orchestrator_cooldown_suspend():
    """provider limit=2/60s，3 并发节点 → 第3个 RESTING 挂起 → tick 等到 1060 唤醒重跑。
    注入假时钟+假 sleep 快进 60s，验证恢复时刻精确 + 全节点最终成功。"""
    print("\n== 冷却挂起→Tick唤醒→重跑（Orchestrator 集成，假时钟快进 60s）==")
    box = {"t": 1000.0}
    fake_clock = lambda: box["t"]

    async def fake_sleep(d):
        box["t"] += d                      # 快进时间，不真等

    raw = {
        "meta": {"name": "cooldown", "version": "0", "tickIntervalSec": 0.5},
        "agents": {f"a{i}": {"id": f"a{i}", "role": "Dev", "provider": "claude",
                              "tools": [], "boundaries": {},
                              "outputs": [{"name": "code", "kind": "text"}]}
                   for i in range(1, 4)},
        "levels": [{"index": 0, "name": "L0", "gate": "all", "nodes": [
            {"id": f"n{i}", "agent": f"a{i}", "task": f"任务{i}"} for i in range(1, 4)
        ]}],
        "qa": {"onFailure": ["console"]},
        "providers": [{"id": "claude", "type": "anthropic",
                        "rate": {"windows": [{"limit": 2, "periodSec": 60}]}}],
    }
    wf = LoadedWorkflow(raw=raw, workflow_id="cooldown", path=Path("."))
    bus = EventBus()
    evs = []
    bus.subscribe(lambda e: evs.append(e.type))
    import tempfile
    tmp = tempfile.mkdtemp(prefix="company-cool-")
    rm = ResourceManager(wf, bus, clock=fake_clock)
    orch = Orchestrator(wf, bus, ProgressLog(tmp),
                        rm=rm, clock=fake_clock, sleep=fake_sleep,
                        checkpoint_root=tmp)
    t0 = box["t"]
    res = await orch.run()
    elapsed = box["t"] - t0

    check("全节点最终成功（挂起者唤醒后重跑通过）",
          res["success"] and all(v["ok"] for v in res["results"].values()),
          f"results={ {k: v['ok'] for k, v in res['results'].items()} }")
    check("触限挂起事件已发", "rate_limit_hit" in evs and "row_suspended" in evs,
          f"events含{sorted(set(evs))}")
    check("tick 唤醒事件已发", "rest_over" in evs)
    check("快进耗时 = 60s（分钟窗口恢复点精确）", abs(elapsed - 60.0) < 1.0,
          f"elapsed={elapsed:.1f}s (t {t0:.0f}→{box['t']:.0f})")


# ===================================================================== 跑
async def main() -> int:
    test_cooldown_time()
    await test_tick()
    test_estimator()
    test_squad()
    test_dept_reject()
    await test_orchestrator_cooldown_suspend()
    failed = [r for r in results if not r[1]]
    print(f"\n{'='*40}\n总计 {len(results)} 项，通过 {len(results)-len(failed)}，失败 {len(failed)}")
    for name, _, detail in failed:
        print(f"  ✗ {name}: {detail}")
    print("全部 PASS ★ Phase 2 资源调度 + 敏捷小队 验收通过" if not failed else "存在失败项")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
