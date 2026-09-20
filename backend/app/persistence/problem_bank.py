"""
problem_bank.py —— Phase 3c 尾：problem_bank 哈希链账本（不可篡改经验沉淀基座）。

架构决策（主人拍板，v1.1.0）：
  存储引擎 = 纯文本 JSONL + SHA256 哈希链（放弃 SQLite）：
    - .company/db/problem_bank.jsonl，append-only，一行一条 JSON 记录
    - hash_i = SHA256(canonical_json_i + prev_hash)；genesis（首条）prev_hash = 64×'0'
    - verify_chain 全量回放重算：任何一行被篡改（改一个标点都算）→ 链立即熔断报错
    - 纯 stdlib（json + hashlib + O_APPEND + flush）；git diff 可读、肉眼看账
    - 单写者模型（FastAPI 单进程引擎 = 唯一 writer，O_APPEND 原子追加，零锁）
  ARCHITECTURE_V2 既定基座："LocalJson（.company/db/*.json，默认）/ Sqlite / BaaS（预留）"——
  本模块即 LocalJson 档落地；DataStore 协议位保留（查询真变重时换实现，引擎零改动）。

记录形态（record）：
  {seq, ts, type, run_id, node_id?, detail, prev_hash, hash}
  type ∈ gate_reject / sop_fail / auth_block / auth_grant / lesson（引擎自动沉淀 + 人工显式）
  seq = 行内单调递增（verify 顺带校验 seq 连续性，防"删中间一条"）
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

GENESIS_HASH = "0" * 64   # 链头哨兵：第一条记录的 prev_hash（"创世块"）

# 记录类型词表（引擎埋点 + 人工沉淀都用这套词汇，审计时可按类型过滤）
RECORD_TYPES = ("gate_reject", "sop_fail", "auth_block", "auth_grant", "lesson")

# 引擎事件 → 账本记录 的映射（自动沉淀三源；auth_grant 由 PR#11 审批通路写入）
_DEPOSIT_MAP: dict[str, str] = {
    "pr_rejected": "gate_reject",
    "sop_fail": "sop_fail",
    "auth_block": "auth_block",
}


def deposit_subscriber(bank: "ProblemBank"):
    """构造 EventBus 订阅器：把红灯教训 / SOP 拦截 / 权限阻断**自动**沉淀进 problem_bank。
    main.py /run 里一行 `bus.subscribe(deposit_subscriber(ProblemBank(root)))` 接上；
    引擎跑出来的"公司踩坑"从此永久留账（哈希链不可篡改，可回放可验账）。"""
    from typing import Any as _Any

    async def _deposit(ev: _Any) -> None:
        rtype = _DEPOSIT_MAP.get(ev.type)
        if rtype is None:
            return
        d = ev.data
        nid = d.get("node_id")
        if rtype == "gate_reject":
            flags = d.get("red_flags") or []
            detail = f"PR {d.get('pr', '?')} rejected: " + ("; ".join(str(x) for x in flags) or "（无明细）")
        else:
            detail = str(d.get("reason", ""))[:4000]
        bank.append(rtype, ev.run_id, node_id=nid, detail=detail)

    return _deposit


class ChainCorruptError(Exception):
    """哈希链熔断：某行哈希不匹配 / prev_hash 不衔接 / seq 断号 / 行非法 JSON。
    账本完整性是物理级保证——verify_chain 是唯一的"验账"入口。"""


def _canonical(record: dict[str, Any]) -> str:
    """参与哈希的规范化 JSON：字段排序 + 紧凑分隔符（消除空格/键序歧义，跨平台稳定）。
    只哈希记录里除 hash 外的全部字段（hash 本身是输出，不参与自身计算）。"""
    body = {k: v for k, v in record.items() if k != "hash"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash_record(record: dict[str, Any]) -> str:
    """record.hash = SHA256(canonical(record 除 hash 外字段) + record.prev_hash)。"""
    payload = _canonical(record) + record["prev_hash"]
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class ChainVerifyReport:
    """verify_chain 结果：全链逐行核对后的验账报告。"""
    ok: bool
    seq_count: int
    broken_seq: Optional[int] = None      # 断裂点（1-based 行序号；None = 全链完好）
    reason: str = ""


class ProblemBank:
    """problem_bank：append-only 哈希链账本（公司踩坑/经验/审批行为的永久沉淀）。

    用法（引擎埋点）：
        bank = ProblemBank(".company")
        await bank.append("gate_reject", run_id, node_id="n2_pr",
                          detail="PR-002 red_flags: pytest L42 …")
        report = bank.verify_chain()          # 全量回放验账（n 很小，O(n)）
        if not report.ok: <大盘账本条标红 + chain_broken 事件>
    """

    def __init__(self, company_root: str | Path):
        """company_root = 项目根（账本落 <root>/.company/db/problem_bank.jsonl）。
        位置对齐 checkpoint/git-sandbox 的 .company/ 约定（LOCAL-FIRST，拷走即全量历史）。"""
        self.path = Path(company_root) / ".company" / "db" / "problem_bank.jsonl"

    # ---- 读 ----
    def records(self) -> list[dict[str, Any]]:
        """全量读（JSONL 逐行）。文件不存在 = 空账本（首跑前）。"""
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        for i, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                raise ChainCorruptError(f"第 {i} 行非法 JSON: {e}") from None
            rec.setdefault("_line", i)
            out.append(rec)
        return out

    def query(self, *, type_: Optional[str] = None, run_id: Optional[str] = None,
              node_id: Optional[str] = None) -> list[dict[str, Any]]:
        """按类型/run/节点过滤（LocalJson 档的查询 = 全量读后筛，体量匹配）。"""
        out = self.records()
        if type_:
            out = [r for r in out if r.get("type") == type_]
        if run_id:
            out = [r for r in out if r.get("run_id") == run_id]
        if node_id:
            out = [r for r in out if r.get("node_id") == node_id]
        return out

    # ---- 写（单写者；O_APPEND 原子追加）----
    def append(self, type_: str, run_id: str, *, node_id: Optional[str] = None,
               detail: str = "", prev_hash: Optional[str] = None) -> dict[str, Any]:
        """追加一条记录：prev_hash 缺省 = 取账本末行 hash（或 genesis），计算本行 hash 后落盘。
        返回完整 record（含 hash，调用方留作证据/事件）。type 不在词表只告警不拦（宽容词汇）。"""
        if type_ not in RECORD_TYPES:
            print(f"[problem_bank] 未知记录类型 {type_!r}（宽容放行，建议入词表）", flush=True)
        base = self.path.parent
        base.mkdir(parents=True, exist_ok=True)
        if prev_hash is None:
            prev_hash = self._last_hash()
        rec: dict[str, Any] = {
            "seq": self._next_seq(),
            "ts": round(time.time(), 3),
            "type": type_,
            "run_id": run_id,
            "detail": detail[:4000],
            "prev_hash": prev_hash,
        }
        if node_id is not None:
            rec["node_id"] = node_id
        rec["hash"] = _hash_record(rec)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            try:
                import os
                os.fsync(f.fileno())      # 账本耐崩溃：append + fsync
            except (OSError, AttributeError):
                pass
        return rec

    def _last_hash(self) -> str:
        recs = self.records()
        return recs[-1]["hash"] if recs else GENESIS_HASH

    def _next_seq(self) -> int:
        recs = self.records()
        return (recs[-1].get("seq", 0) + 1) if recs else 1

    # ---- 验账（哈希链的黄金价值：篡改一个标点即熔断）----
    def verify_chain(self) -> ChainVerifyReport:
        """全量回放：逐行重算 SHA256 + 核对 prev_hash 衔接 + seq 连续。
        任何一行被改（detail 一个标点 / 换行序 / 删中间一条）→ 返回 ok=False + 断点 + 原因。"""
        seq = 0
        expected_prev = GENESIS_HASH
        try:
            for line_no, line in enumerate(
                    self.path.read_text(encoding="utf-8").splitlines() if self.path.exists() else [], 1):
                if not line.strip():
                    continue
                rec = json.loads(line)
                seq += 1
                if rec.get("seq") != seq:
                    return ChainVerifyReport(False, seq - 1, seq,
                                              f"seq 断号（第 {line_no} 行 seq={rec.get('seq')} ≠ 期望 {seq}，疑删中间行）")
                if rec.get("prev_hash") != expected_prev:
                    return ChainVerifyReport(False, seq - 1, seq,
                                              f"prev_hash 不衔接（第 {line_no} 行，疑改写过历史）")
                stored = rec.get("hash", "")
                recomputed = _hash_record(rec)
                if stored != recomputed:
                    # 熔断断点 broken_seq = 被篡改行自身 seq（大盘"哪条账红了"定位用）
                    return ChainVerifyReport(False, seq - 1, seq,
                                              f"hash 不匹配（第 {line_no} 行 seq={seq} 被篡改：改动了字段值）")
                expected_prev = stored
        except json.JSONDecodeError as e:
            return ChainVerifyReport(False, seq, seq + 1, f"第 {line_no} 行非法 JSON: {e}")
        return ChainVerifyReport(True, seq)
