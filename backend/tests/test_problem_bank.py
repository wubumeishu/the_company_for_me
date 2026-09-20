"""Phase 3c 尾 单测：problem_bank 哈希链账本（不可篡改经验沉淀基座）。

跑法：cd backend && python tests/test_problem_bank.py
全离线（纯文件操作，零 git 零进程）：
  A) 追加与读回：genesis / prev_hash 衔接 / seq 单调 / 幂等 verify
  B) ★ 暴力篡改检测（哈希链含金量证明）：
     B-① 改某行 detail 的一个标点 → hash 不匹配，熔断且断点定位到该行
     B-② 删中间一行 → seq 断号 / prev_hash 不衔接，熔断
     B-③ 改某行 prev_hash 本身 → 熔断
     B-④ 追加合法新行 → 全链再验仍绿（账本可持续生长，非一次性封印）
  C) 引擎埋点形态：gate_reject/sop_fail/auth_block/auth_grant 四类沉淀 + 按类型过滤查询
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.persistence.problem_bank import ProblemBank, GENESIS_HASH, ChainCorruptError, deposit_subscriber

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, cond, detail))
    print(f"  {PASS if cond else FAIL}  {name}  {detail}")


def _bank() -> tuple[ProblemBank, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="company-bank-"))
    return ProblemBank(tmp), tmp


# ============ A) 追加与读回 ============
def test_append_readback():
    print("\n== A) 追加与读回（genesis / 衔接 / seq / 幂等 verify） ==")
    bank, tmp = _bank()
    r1 = bank.append("lesson", "run-a", detail="首条经验")
    check("genesis 记录 prev_hash = 64×0", r1["prev_hash"] == GENESIS_HASH)
    check("genesis seq = 1", r1["seq"] == 1)
    r2 = bank.append("gate_reject", "run-a", node_id="n2_pr", detail="PR-002 红灯")
    check("第二条 prev_hash = 第一条 hash（链式衔接）", r2["prev_hash"] == r1["hash"])
    check("seq 单调（2）", r2["seq"] == 2)
    r3 = bank.append("sop_fail", "run-a", node_id="n1", detail="dev_handoff 缺 self_check")
    rep = bank.verify_chain()
    check("全链 verify 通过（3 条）", rep.ok and rep.seq_count == 3, f"{rep}")
    rep2 = bank.verify_chain()
    check("verify 幂等（重复验账结果不变）", rep2.ok and rep2.seq_count == 3)
    check("空账本（新文件）verify 通过", ProblemBank(Path(tempfile.mkdtemp())).verify_chain().ok)
    # 行落盘形态（JSONL 每行一条，可读可 diff）
    lines = (tmp / ".company" / "db" / "problem_bank.jsonl").read_text(encoding="utf-8").splitlines()
    check("JSONL 落盘 3 行且每行合法 JSON", len(lines) == 3 and all(json.loads(l) for l in lines))


# ============ B) ★ 暴力篡改检测 ============
def _corrupt_line(bank_path: Path, line_no: int, mutator) -> None:
    """把第 line_no 行（1-based）交给 mutator(rec_dict) 改，写回（模拟人为/恶意篡改）。"""
    lines = bank_path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[line_no - 1])
    mutator(rec)
    lines[line_no - 1] = json.dumps(rec, ensure_ascii=False)
    bank_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_tamper_detection():
    print("\n== B) ★ 暴力篡改检测（哈希链含金量） ==")
    bank, tmp = _bank()
    p = tmp / ".company" / "db" / "problem_bank.jsonl"
    for i, t in enumerate(["lesson", "gate_reject", "auth_block"], 1):
        bank.append(t, f"run-{i}", detail=f"第{i}条")

    # B-① 改中间一行 detail 的【一个标点】→ 该行 hash 必断
    _corrupt_line(p, 2, lambda r: r.update(detail="PR-002 红灯！"))   # 末尾加了个"！"
    rep = bank.verify_chain()
    check("★ 改 1 个标点 → verify 立即熔断", not rep.ok, rep.reason)
    check("熔断断点定位到被改行（seq=2）", rep.broken_seq == 2, f"broken_seq={rep.broken_seq}")
    check("熔断原因 = hash 不匹配（篡改证据）", "hash 不匹配" in rep.reason, rep.reason)

    # B-② 删中间一行 → seq 断号 / 衔接断裂（账本不能少页）
    bank2, tmp2 = _bank()
    p2 = tmp2 / ".company" / "db" / "problem_bank.jsonl"
    for i in range(1, 5):
        bank2.append("lesson", f"run-{i}", detail=f"第{i}条")
    lines = p2.read_text(encoding="utf-8").splitlines()
    del lines[1]                       # 删掉第 2 行
    p2.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rep2 = bank2.verify_chain()
    check("★ 删中间一行 → verify 熔断（seq 断号/衔接断裂）", not rep2.ok, rep2.reason)

    # B-③ 直接改某行的 prev_hash 字段本身 → 也熔断
    bank3, tmp3 = _bank()
    p3 = tmp3 / ".company" / "db" / "problem_bank.jsonl"
    bank3.append("lesson", "run-1", detail="a")
    bank3.append("lesson", "run-2", detail="b")
    _corrupt_line(p3, 2, lambda r: r.update(prev_hash="f" * 64))
    rep3 = bank3.verify_chain()
    check("★ 改 prev_hash 字段本身 → verify 熔断", not rep3.ok, rep3.reason)

    # B-④ 篡改后追加合法新行 → 全链再验仍红（篡改不可被"续写"洗白）
    rep4 = bank3.append("lesson", "run-3", detail="c") is not None and not bank3.verify_chain().ok
    check("★ 篡改后续写新行不能洗白（链永久带伤）", rep4)


# ============ C) 引擎埋点形态（四类沉淀 + 查询） ============
def test_engine_deposit_shapes():
    print("\n== C) 引擎埋点形态（自动沉淀四类 + 按类型/run/节点过滤） ==")
    bank, _ = _bank()
    bank.append("gate_reject", "run-x", node_id="n2_pr",
                detail="PR-003 rejected: red_flags=[pytest L42 assert None is not None]")
    bank.append("sop_fail", "run-x", node_id="n1",
                detail="dev_handoff.self_check.passed 缺失")
    bank.append("auth_block", "run-x", node_id="n3",
                detail="external send_email 未授权（auth_block）")
    bank.append("auth_grant", "run-x", node_id="n3",
                detail="人类公章：approval_token 签发放行 send_email")
    check("四类记录各一条", bank.query(type_="gate_reject") and bank.query(type_="sop_fail")
          and bank.query(type_="auth_block") and bank.query(type_="auth_grant"))
    check("按 run 过滤", len(bank.query(run_id="run-x")) == 4)
    check("按节点过滤（n3 两条：block+grant 全留痕）", len(bank.query(node_id="n3")) == 2)
    check("全链 verify 通过（4 条）", bank.verify_chain().ok)
    # 未知类型宽容放行（词表外告警但不拦——审计词汇可演进）
    r = bank.append("custom_type", "run-x", detail="宽容档")
    check("未知类型宽容追加（不熔断）", bank.verify_chain().ok)


# ============ D) 真实 EventBus 自动沉淀（引擎红灯 → 账本自动留痕） ============
async def test_event_bus_autodeposit():
    print("\n== D) 真 EventBus 联动（pr_rejected/sop_fail/auth_block 自动沉淀） ==")
    from app.engine.events import EventBus
    bank, tmp = _bank()
    bus = EventBus(run_id="run-live")
    bus.subscribe(deposit_subscriber(bank))

    # 引擎三源事件（字段形态与 3b 引擎实发一致）
    await bus.publish("pr_rejected", pr="PR-002", red_flags=["pytest L42 assert None"],
                      node_id="n2_pr", backend="subprocess")
    await bus.publish("sop_fail", node_id="n1", reason="dev_handoff.self_check.passed 缺失")
    await bus.publish("auth_block", node_id="n3", reason="external send_email 未授权")
    # 无关事件（git_commit 等）不入账
    await bus.publish("git_commit", node_id="n1", branch="dev-a", sha="abc", msg="x")

    check("三源事件自动沉淀 3 条", len(bank.records()) == 3, f"len={len(bank.records())}")
    check("gate_reject 1 条（含 PR 号与 red_flags 明细）",
          any(r["type"] == "gate_reject" and "PR-002" in r["detail"] and "pytest L42" in r["detail"]
              for r in bank.records()))
    check("sop_fail / auth_block 各 1 条",
          len(bank.query(type_="sop_fail")) == 1 and len(bank.query(type_="auth_block")) == 1)
    check("沉淀后全链 verify 仍绿", bank.verify_chain().ok)


def main() -> int:
    test_append_readback()
    test_tamper_detection()
    test_engine_deposit_shapes()
    import asyncio
    asyncio.run(test_event_bus_autodeposit())
    failed = [r for r in results if not r[1]]
    print(f"\n{'='*44}\n总计 {len(results)} 项，通过 {len(results)-len(failed)}，失败 {len(failed)}")
    for name, _, detail in failed:
        print(f"  ✗ {name}: {detail}")
    print("全部 PASS ★ Phase 3c 尾 problem_bank 哈希链账本（篡改即熔断）验收通过"
          if not failed else "存在失败项")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
