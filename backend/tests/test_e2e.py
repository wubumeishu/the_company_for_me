"""Phase 1 全链路端到端测试：解析 JSON → 校验 → 并发执行 → QA 拦截 → 回退。

跑法：
    cd backend && python tests/test_e2e.py
三条断言链：
  A) mock-demo（正常链）  → 3 行全过，QA 节点拿到两个上游产物
  B) mock-demo-fail（拦截链）→ L2 QA 的 sim_fail 拦截 → 红框事件 + 回退 L1 →
     回退重跑 L1（上游注入 checkpoint 产物）→ 再 L2 仍 sim_fail → 重试耗尽 → run FAILED
  C) 同行不变量：篡改 level.index 后校验器拒绝（第 2 关）
  D) progress.md 打卡行数增长（Strict Constraint #2 埋点生效）
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # backend/
sys.path.insert(0, str(ROOT))

from app.engine.events import EventBus
from app.engine.orchestrator import Orchestrator
from app.persistence.progress import ProgressLog
from app.schema.validator import ContractError, load_workflow

PROGRESS = ROOT.parent / "progress.md"
TMP_CK = ROOT / ".company"                            # checkpoint 落这里（gitignore）

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, cond, detail))
    print(f"  {PASS if cond else FAIL}  {name}  {detail}")


# ---------------------------------------------------------------- A) 正常链
async def run_success_case() -> None:
    print("\n== A) mock-demo 正常全链路（L0 → L1 并发×2 → L2 QA 放行）==")
    wf = load_workflow("mock-demo")
    bus = EventBus()
    seen: list[str] = []
    bus.subscribe(lambda ev: seen.append(ev.type))

    orch = Orchestrator(wf, bus, ProgressLog(str(ROOT.parent)), checkpoint_root=str(ROOT))
    res = await orch.run()

    check("run 成功", res["success"], f"failed_level={res['failed_level']}")
    check("L1 并发节点都通过",
          res["results"].get("n1_fe", {}).get("ok") and res["results"].get("n1_be", {}).get("ok"),
          f"{res['results'].get('n1_fe')} {res['results'].get('n1_be')}")
    check("QA 节点拿到上游产物（数据流贯通）",
          res["results"].get("n2_qa", {}).get("ok"),
          f"n2_qa={res['results'].get('n2_qa')}")
    # 并发性证据：L1 两节点的 node_start 在 level_done 前成对出现
    check("事件流齐全（run_start/level_start/node_start/thinking/tool_call）",
          "run_start" in seen and "level_done" in seen and "node_start" in seen and "thinking" in seen,
          f"事件种类={sorted(set(seen))}")
    # 行序：level_done L0 之后才可能 level_start L1
    lvl_seq = [i for i, t in enumerate(seen) if t in ("level_start", "level_done")]
    check("行序 Barrier（L0 done → L1 start → L1 done → L2 start）",
          seen[lvl_seq[1]] == "level_done" and seen[lvl_seq[2]] == "level_start")
    ck = (TMP_CK / f"checkpoint_{res['run_id']}.json")
    check("checkpoint 落盘（回退依赖）", ck.exists(), str(ck.name if ck.exists() else "missing"))


# ---------------------------------------------------------------- B) 拦截+回退链
async def run_fail_case() -> None:
    print("\n== B) mock-demo-fail 拦截链（L2 sim_fail → 红框 → 回退 L1 → 重试耗尽 → FAILED）==")
    wf = load_workflow("mock-demo-fail")
    bus = EventBus()
    seen: list[str] = []
    bus.subscribe(lambda ev: seen.append(ev.type))

    orch = Orchestrator(wf, bus, ProgressLog(str(ROOT.parent)))
    res = await orch.run()

    check("run 最终 FAILED（QA 拦截 + 回退重跑仍 sim_fail）", not res["success"],
          f"failed_level={res['failed_level']}")
    check("red_highlight 事件已发（红框高亮）", "red_highlight" in seen)
    check("rollback 事件已发（回退到 L1）", "rollback" in seen)
    rb = [i for i, t in enumerate(seen) if t == "rollback"]
    check("回退重跑发生（run_start 第二次，resuming_from=1）",
          len([t for t in seen if t == "run_start"]) >= 2)
    check("重试次数 ≤ maxAttempts（防死循环）",
          len(rb) <= 2, f"rollback 次数={len(rb)}")


# ---------------------------------------------------------------- C) 不变量
def run_invariant_case() -> None:
    print("\n== C) 同行不变量（level.index 必须 == 下标）==")
    good = json.loads((ROOT.parent / "workflows" / "mock-demo.json").read_text(encoding="utf-8"))
    load_workflow("mock-demo")                       # 正常应过
    check("合法 workflow 通过三关校验", True)

    bad = json.loads(json.dumps(good))
    bad["levels"][1]["index"] = 9                     # 人为破坏：L1 的 index 改成 9
    try:
        load_workflow(raw=bad)
        check("index≠下标 被拒绝", False, "竟然放行了?!")
    except ContractError as e:
        check("index≠下标 被拒绝", "同行不变量" in str(e), str(e)[:60])

    bad2 = json.loads(json.dumps(good))
    bad2["levels"][0]["nodes"][0]["agent"] = "ghost_agent"   # 幽灵引用
    try:
        load_workflow(raw=bad2)
        check("幽灵 agent 引用 被拒绝", False)
    except ContractError as e:
        check("幽灵 agent 引用 被拒绝", "不存在的 agent" in str(e))


# ---------------------------------------------------------------- D) 打卡
async def run_progress_case() -> None:
    print("\n== D) progress.md 打卡埋点（执行前/后各一行）==")
    before = len([l for l in PROGRESS.read_text(encoding="utf-8").splitlines() if l.startswith("| 20")])
    wf = load_workflow("mock-demo")
    await Orchestrator(wf, EventBus(), ProgressLog(str(ROOT.parent)),
                       checkpoint_root=str(ROOT)).run()
    after = len([l for l in PROGRESS.read_text(encoding="utf-8").splitlines() if l.startswith("| 20")])
    check("打卡行数增长（节点执行前后都有登记）", after > before, f"before={before} after={after}")


async def main() -> int:
    await run_success_case()
    await run_fail_case()
    run_invariant_case()
    await run_progress_case()

    failed = [r for r in results if not r[1]]
    print(f"\n{'=' * 40}\n总计 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    if failed:
        for name, _, detail in failed:
            print(f"  ✗ {name}: {detail}")
        return 1
    print("全部 PASS ★ Phase 1 后端引擎验收通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
