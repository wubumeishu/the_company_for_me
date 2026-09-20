"""Phase 3c 尾·② 单测：WS 审批真通路（人类公章，§1.5）+ 宪法校验 + 账本留痕。

跑法：cd backend && python tests/test_approval_flow.py
全离线（零真网络）：
  A) Broker 纯单测：HMAC 三关（真伪/工具绑定/nonce 一次性）+ TTL + 拒绝 + 超时 + 篡改令牌
  B) ★ 全链路（主人点名的那条链）：Agent 调 external 工具 → 引擎 Wait 挂起发
     approval_request → 模拟人类批准签 HMAC 令牌 → 引擎 verify 通过解挂并继续执行
  C) 真实 load_workflow 宪法校验：新 schema 字段（external 工具词表 + boundaries 4 字段）
     过四关；非法 approval 枚举被 schema 拒绝
  D) problem_bank 联动：auth_grant 公章行为自动留哈希账本 + 全链 verify 仍绿 + 拒绝链 auth_block 留账
  E) 零回归：agent 缺省 approval（=fail 档）→ external 无令牌立即 AuthGateError（旧行为不变）
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.engine.approval import ApprovalBroker
from app.engine.events import EventBus
from app.schema.validator import LoadedWorkflow

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, cond, detail))
    print(f"  {PASS if cond else FAIL}  {name}  {detail}")


class Tracker:
    """EventBus 事件跟踪器：同步订阅（EngineBus.publish 对同步订阅者直接调用）。"""

    def __init__(self, bus: EventBus):
        self.counts: dict[str, int] = {}
        self.events: list = []
        self.last_rid: str | None = None

        def _sub(ev):
            self.events.append(ev)
            self.counts[ev.type] = self.counts.get(ev.type, 0) + 1
            if ev.type == "approval_request":
                self.last_rid = ev.data.get("request_id")
        bus.subscribe(_sub)

    def count(self, t: str) -> int:
        return self.counts.get(t, 0)


async def _wait_until(cond, timeout: float = 8.0) -> None:
    """等到 cond() 为真（带超时防死等——挂起类 bug 必须显性失败，不许静默挂住测试）。"""
    waited = 0.0
    while not cond():
        await asyncio.sleep(0.01)
        waited += 0.01
        if waited > timeout:
            raise TimeoutError(f"等待超时（{timeout}s）：{cond!r}")


def _mailer_agent(approval: str | None) -> dict:
    """高风险 external 工具 send_email + grant；approval 档可配（wait / None=缺省 fail）。"""
    b = {"externalGrants": ["send_email"], "sideEffects": {"send_email": "external_side"}}
    if approval:
        b["approval"] = approval
    return {"id": "mailer", "role": "MailOps", "provider": "mock",
            "tools": ["send_email"], "boundaries": b,
            "outputs": [{"name": "report", "kind": "text"}]}


def _raw_wf(agent: dict) -> dict:
    return {
        "meta": {"name": "appr-flow", "version": "0"},
        "agents": {"mailer": agent},
        "levels": [{"index": 0, "name": "L0", "nodes": [
            {"id": "n1", "agent": "mailer", "task": "发高风险邮件",
             "toolOverrides": ["send_email"]}]},
        ],
        "qa": {"onFailure": ["console"]},
    }


def _wf_stub(agent: dict) -> LoadedWorkflow:
    return LoadedWorkflow(raw=_raw_wf(agent), workflow_id="stub", path=Path("."))


# ============ A) Broker 纯单测（HMAC 三关 + 防重放 + 拒绝/超时） ============
async def test_broker() -> None:
    print("\n== A) ApprovalBroker（HMAC 三关 + 防重放） ==")
    broker = ApprovalBroker()
    bus = EventBus()
    T = Tracker(bus)

    # 挂起 → 批准 → 解挂
    task = asyncio.create_task(broker.request("n1", "send_email", "external_side", "高风险未授权", bus))
    await _wait_until(lambda: T.last_rid is not None)
    tok = broker.approve(T.last_rid)
    ok, token = await asyncio.wait_for(task, 5)
    check("挂起→批准→解挂（放行 + 返回令牌）", ok and token == tok, f"ok={ok}")
    check("approval_granted 事件已发", T.count("approval_granted") == 1)
    check("verify_token 三关全过（HMAC+工具绑定+nonce+TTL）", broker.verify_token("send_email", tok))
    check("★ nonce 一次性：同令牌第二次必拒（防重放）", not broker.verify_token("send_email", tok))

    # 工具绑定：push_remote 的令牌验 send_email 必拒
    task2 = asyncio.create_task(broker.request("n1", "push_remote", "external_side", "r", bus))
    await _wait_until(lambda: T.count("approval_request") == 2)
    tok2 = broker.approve(T.last_rid)
    ok2, _ = await asyncio.wait_for(task2, 5)
    check("★ 工具绑定：push_remote 令牌验 send_email 必拒",
          ok2 and not broker.verify_token("send_email", tok2))

    # 篡改签名 → 拒
    tampered = tok[:-2] + ("AA" if tok[-2:] != "AA" else "BB")
    check("★ 篡改令牌签名 → verify 拒", not broker.verify_token("send_email", tampered))

    # TTL 过期档（ttl=-1 = 即刻过期）
    exp = ApprovalBroker(token_ttl=-1.0, request_timeout=5)
    bus2, T2 = EventBus(), None
    T2 = Tracker(bus2)
    task3 = asyncio.create_task(exp.request("n1", "send_email", "external_side", "r", bus2))
    await _wait_until(lambda: T2.last_rid is not None)
    tok3 = exp.approve(T2.last_rid)
    ok3, _ = await asyncio.wait_for(task3, 5)
    check("★ TTL 过期 → verify 拒（令牌时效物理保证）", not exp.verify_token("send_email", tok3))

    # 拒绝通路
    task4 = asyncio.create_task(broker.request("n1", "send_email", "external_side", "r", bus))
    await _wait_until(lambda: T.count("approval_request") == 3)
    broker.deny(T.last_rid, reason="人类拒绝")
    ok4, tok4 = await asyncio.wait_for(task4, 5)
    check("拒绝 → 不放行 + 无令牌 + approval_denied 事件",
          (not ok4) and tok4 == "" and T.count("approval_denied") == 1)

    # 超时通路（人类没来 = 按拒绝）
    fast = ApprovalBroker(request_timeout=0.2)
    bus3 = EventBus()
    T3 = Tracker(bus3)
    task5 = asyncio.create_task(fast.request("n1", "send_email", "external_side", "r", bus3))
    await _wait_until(lambda: T3.last_rid is not None)
    ok5, _ = await asyncio.wait_for(task5, 5)
    check("超时 → 按拒绝 + approval_timeout 事件",
          not ok5 and T3.count("approval_timeout") == 1)

    # pending_ids（大盘"迟滞榜·审批中"数据源）
    task6 = asyncio.create_task(fast.request("n1", "send_email", "external_side", "r", bus3))
    await _wait_until(lambda: T3.count("approval_request") == 2)
    rid6 = T3.last_rid
    check("挂起中 pending_ids 可查", rid6 in fast.pending_ids())
    fast.deny(rid6)
    await asyncio.wait_for(task6, 5)
    check("解挂后 pending_ids 清空", len(fast.pending_ids()) == 0)


# ============ B) ★ 全链路：Agent external 调用 → Wait 挂起 → 人类批准 → 解挂继续执行 ============
async def test_fullchain() -> None:
    print("\n== B) ★ 全链路（Agent 挂起 → 人类公章签发放行 → 解挂执行） ==")
    from app.persistence.progress import ProgressLog
    from app.engine.executor import NodeExecutor

    tmp = Path(tempfile.mkdtemp(prefix="company-appr-"))
    bus = EventBus()
    T = Tracker(bus)
    broker = ApprovalBroker(request_timeout=10)
    ex = NodeExecutor(node=_raw_wf(_mailer_agent("wait"))["levels"][0]["nodes"][0],
                      agent=_mailer_agent("wait"), wf=_wf_stub(_mailer_agent("wait")), bus=bus,
                      progress=ProgressLog(str(tmp)), approval_broker=broker)
    ex.level = 0

    task = asyncio.create_task(ex.run())
    await _wait_until(lambda: T.last_rid is not None)      # 等引擎挂起（approval_request）
    check("★ 引擎挂起：external 调用触发 approval_request（Wait 断点）",
          T.count("approval_request") == 1)
    check("★ 挂起期间工具未放行（尚无 tool_authorize allowed=True）",
          T.count("tool_authorize") == 0, f"tool_authorize={T.count('tool_authorize')}")

    broker.approve(T.last_rid)   # ← 模拟人类点「批准」（与前端 POST /approval 同款动作）
    out = await asyncio.wait_for(task, 10)
    check("★ 解挂：HMAC 令牌校验通过 → 工具放行（tool_authorize allowed=True）",
          out.ok and T.count("tool_authorize") >= 1, f"ok={out.ok}")
    check("★ 解挂后 Agent 继续执行完成（产物已出，不再卡）",
          out.ok and bool(out.outputs), str(out.outputs)[:60])
    check("auth_grant 公章事件已发（进哈希账本）", T.count("auth_grant") == 1)


# ============ C) 真实 load_workflow 宪法校验 ============
def test_constitution() -> None:
    print("\n== C) 宪法校验（新 schema 字段过四关 + 非法枚举拒绝） ==")
    from app.schema.validator import load_workflow, ContractError
    ok = load_workflow(raw=_raw_wf(_mailer_agent("wait")))
    check("external 工具 + boundaries 4 字段（approval=wait）过 load_workflow 四关", ok is not None)
    try:
        load_workflow(raw=_raw_wf(_mailer_agent("bogus")))
        check("非法 approval 枚举被 schema 拒绝", False, "竟然放行?!")
    except ContractError as e:
        check("非法 approval 枚举被 schema 拒绝", "不符合" in str(e) or "approval" in str(e),
              str(e)[:60])
    # 部门级词汇表（org.py TOOL_VOCAB）external 工具入表（dict 形态：key=部门 id，object 内不再放 id）
    raw_dept = {
        "meta": {"name": "d", "version": "0"},
        "agents": {"ops1": {"id": "ops1", "role": "O", "provider": "mock",
                            "tools": ["send_email"], "boundaries": {}, "department": "ops"}},
        "departments": {"ops": {"tools": ["send_email", "push_remote"]}},
        "levels": [{"index": 0, "name": "L0", "nodes": [
            {"id": "n1", "agent": "ops1", "task": "x"}]}],
        "qa": {"onFailure": ["console"]},
    }
    ok2 = load_workflow(raw=raw_dept)
    check("部门级 external 工具（send_email/push_remote）过 org 词汇表校验", ok2 is not None)


# ============ D) problem_bank 联动（公章/拒绝行为留痕） ============
async def test_bank_deposit() -> None:
    print("\n== D) problem_bank 联动（auth_grant / auth_block 自动留哈希账本） ==")
    from app.persistence.problem_bank import ProblemBank, deposit_subscriber
    from app.persistence.progress import ProgressLog
    from app.engine.executor import NodeExecutor

    # D-① 批准链：auth_grant 自动入账
    tmp = Path(tempfile.mkdtemp(prefix="company-apprbank-"))
    bank = ProblemBank(tmp)
    bus = EventBus()
    bus.subscribe(deposit_subscriber(bank))
    T = Tracker(bus)
    broker = ApprovalBroker(request_timeout=8)
    ex = NodeExecutor(node=_raw_wf(_mailer_agent("wait"))["levels"][0]["nodes"][0],
                      agent=_mailer_agent("wait"), wf=_wf_stub(_mailer_agent("wait")), bus=bus,
                      progress=ProgressLog(str(tmp)), approval_broker=broker)
    ex.level = 0
    task = asyncio.create_task(ex.run())
    await _wait_until(lambda: T.last_rid is not None)
    broker.approve(T.last_rid)
    out = await asyncio.wait_for(task, 8)
    grants = bank.query(type_="auth_grant")
    check("批准链：auth_grant 自动沉淀账本（公章行为留痕）", len(grants) == 1,
          f"{len(grants)} 条" + (f" detail={grants[0]['detail'][:40]}" if grants else ""))
    check("沉淀后全链 verify 仍绿", bank.verify_chain().ok)

    # D-② 拒绝链：auth_block 自动入账（executor AuthGateError → auth_block 事件 → 沉淀）
    tmp2 = Path(tempfile.mkdtemp(prefix="company-apprbank2-"))
    bank2 = ProblemBank(tmp2)
    bus2 = EventBus()
    bus2.subscribe(deposit_subscriber(bank2))
    T2 = Tracker(bus2)
    broker2 = ApprovalBroker(request_timeout=8)
    ex2 = NodeExecutor(node=_raw_wf(_mailer_agent("wait"))["levels"][0]["nodes"][0],
                       agent=_mailer_agent("wait"), wf=_wf_stub(_mailer_agent("wait")), bus=bus2,
                       progress=ProgressLog(str(tmp2)), approval_broker=broker2)
    ex2.level = 0
    task2 = asyncio.create_task(ex2.run())
    await _wait_until(lambda: T2.last_rid is not None)
    broker2.deny(T2.last_rid, reason="人类拒绝")
    out2 = await asyncio.wait_for(task2, 8)
    blocks = bank2.query(type_="auth_block")
    check("★ 拒绝链：auth_block 自动沉淀账本（公章拒绝也留痕）",
          len(blocks) == 1 and not out2.ok, f"{len(blocks)} 条 ok={out2.ok}")
    check("拒绝后全链 verify 仍绿", bank2.verify_chain().ok)


# ============ E) 零回归：缺省 approval=fail ============
async def test_zero_regression() -> None:
    print("\n== E) 零回归（缺省 fail 档：无 wait/broker → 立即 AuthGateError 旧行为） ==")
    from app.persistence.progress import ProgressLog
    from app.engine.executor import NodeExecutor
    tmp = Path(tempfile.mkdtemp(prefix="company-apprzr-"))
    bus = EventBus()
    T = Tracker(bus)
    ex = NodeExecutor(node=_raw_wf(_mailer_agent(None))["levels"][0]["nodes"][0],
                      agent=_mailer_agent(None), wf=_wf_stub(_mailer_agent(None)), bus=bus,
                      progress=ProgressLog(str(tmp)))     # 无 approval_broker + 无 wait 档
    ex.level = 0
    out = await ex.run()
    check("★ 缺省档零回归：external 高风险无令牌 → 节点 FAIL（旧即时拒绝路径）",
          not out.ok and "授权闸门" in (out.error or ""), out.error or "")
    check("auth_block 事件已发（可沉淀可回放）", T.count("auth_block") == 1)
    check("零回归链不产生 approval_request（从未挂起等人类）", T.count("approval_request") == 0)


async def main() -> int:
    await test_broker()
    await test_fullchain()
    test_constitution()
    await test_bank_deposit()
    await test_zero_regression()
    failed = [r for r in results if not r[1]]
    print(f"\n{'='*44}\n总计 {len(results)} 项，通过 {len(results)-len(failed)}，失败 {len(failed)}")
    for name, _, detail in failed:
        print(f"  ✗ {name}: {detail}")
    print("全部 PASS ★ Phase 3c 尾② WS 审批真通路（人类公章）+ 宪法 + 账本 验收通过"
          if not failed else "存在失败项")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
