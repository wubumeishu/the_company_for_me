"""
approval.py —— Phase 3c 尾·②：人类审批断点的真通路（§1.5 公章，Agency-Agents human-in-the-loop）。

主人拍板的设计（v1.1.0）：
  引擎：高风险 external_side 工具（HIGH_RISK_TOOLS）+ agent.boundaries.approval == "wait"
        → executor 不立即 FAIL，而是发 approval_request 事件 + 挂起（await 一个 asyncio.Event）
  前端：ApprovalModal 收到 approval_request → 小窗亮 → 人类点「批准」
        → POST /approval {run_id, request_id, decision}
  引擎：broker.approve 签发 **HMAC-SHA256 加密令牌**（nonce 一次性 + 时间戳 + TTL 防重放）
        → executor 拿到令牌后 **verify_token 再验一遍**（HMAC 校验 + nonce 未用 + TTL 未过）
        → 校验通过才放行该工具调用 + 发 auth_grant（自动沉淀进 problem_bank 哈希账本，PR#10）
  拒绝/超时：approval_denied / approval_timeout → auth_block 留痕 + 节点 FAIL（锁链保持）

零回归保证：agent 未声明 boundaries.approval="wait"（缺省 "fail"）→ 走既有
authorize() 即时拒绝路径（AuthGateError），本模块完全不介入。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
import uuid
from typing import Any, Optional

from .events import EventBus

# 令牌默认 TTL（秒）：签发的审批令牌只在这个窗口内有效（防"旧令牌被拿来重放"）
DEFAULT_TOKEN_TTL = 300.0
# 挂起等待默认上限（秒）：人类不在 → 超时按拒绝处理（流程不永久卡死）
DEFAULT_REQUEST_TIMEOUT = 600.0


class ApprovalBroker:
    """一次 run 的审批协调器：请求挂起 / 签发令牌 / 校验令牌（nonce 一次性 + TTL）。

    挂在 main.py 的 RUNS[run_id] 上，经 orchestrator → executor 传下去；
    令牌 = base64url(nonce:ts) + "." + HMAC-SHA256(key, f"{tool}:{nonce}:{ts}")
    校验三关：HMAC 恒定时间比对 + nonce 未消费过 + 未超 TTL（全本地，密钥不出进程）。
    """

    def __init__(self, *, key: Optional[bytes] = None,
                 token_ttl: float = DEFAULT_TOKEN_TTL,
                 request_timeout: float = DEFAULT_REQUEST_TIMEOUT):
        self._key = key or secrets.token_bytes(32)   # 进程级随机密钥（LOCAL-FIRST 公章印泥）
        self._token_ttl = token_ttl
        self._request_timeout = request_timeout
        self._pending: dict[str, Any] = {}           # request_id → {"event": asyncio.Event, "granted": bool, "token": str}
        self._nonce_used: set[str] = set()           # 已消费 nonce（一次性防重放）

    # ---- 引擎侧：请求 + 挂起等待（executor 调）----
    async def request(self, node_id: str, tool: str, side_effect: str, reason: str,
                      bus: EventBus) -> tuple[bool, str]:
        """发 approval_request → 挂起等人类决定（超时=拒绝）。返回 (放行?, 令牌)。"""
        import asyncio
        request_id = f"appr-{uuid.uuid4().hex[:8]}"
        ev = asyncio.Event()
        self._pending[request_id] = {"event": ev, "granted": False, "token": "", "tool": tool}
        await bus.publish("approval_request", request_id=request_id, node_id=node_id,
                          tool=tool, side_effect=side_effect, reason=reason,
                          timeout_sec=self._request_timeout)
        try:
            await asyncio.wait_for(ev.wait(), timeout=self._request_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            await bus.publish("approval_timeout", request_id=request_id, node_id=node_id,
                              tool=tool, timeout_sec=self._request_timeout)
            return False, ""
        entry = self._pending.pop(request_id, {})
        if not entry.get("granted"):
            await bus.publish("approval_denied", request_id=request_id, node_id=node_id,
                              tool=tool, reason=entry.get("reason", "人类拒绝"))
            return False, ""
        await bus.publish("approval_granted", request_id=request_id, node_id=node_id, tool=tool)
        return True, entry.get("token", "")

    # ---- 前端侧：人类决定（main.py POST /approval 调）----
    def approve(self, request_id: str) -> str:
        """签发 HMAC 令牌（nonce 一次性 + 时间戳 + TTL）并放行挂起的引擎。
        tool 名存于请求条目（令牌绑定该工具，verify 时再核对），API 侧无需传。"""
        entry = self._pending.get(request_id)
        if entry is None:
            raise KeyError(f"未知/已失效的审批请求 {request_id}")
        tool = entry["tool"]
        nonce = uuid.uuid4().hex
        ts = int(time.time())
        payload = f"{tool}:{nonce}:{ts}".encode("utf-8")
        sig = hmac.new(self._key, payload, hashlib.sha256).digest()
        token = f"{base64.urlsafe_b64encode(payload).decode().rstrip('=')}" \
                f".{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"
        entry["granted"] = True
        entry["token"] = token
        entry["event"].set()
        return token

    def deny(self, request_id: str, reason: str = "人类拒绝") -> None:
        entry = self._pending.get(request_id)
        if entry is None:
            raise KeyError(f"未知/已失效的审批请求 {request_id}")
        entry["granted"] = False
        entry["reason"] = reason
        entry["event"].set()

    def pending_ids(self) -> list[str]:
        """当前挂起等待的审批请求（大盘"迟滞榜·审批中"数据源）。"""
        return [rid for rid, e in self._pending.items() if not e.get("granted")]

    # ---- 引擎侧：令牌再验（"校验通过才放行"的物理保证）----
    def verify_token(self, tool: str, token: str) -> bool:
        """三关全过才放行：HMAC 恒定时间比对 + nonce 未消费（一次性）+ 未超 TTL。"""
        try:
            b64, b64sig = token.split(".")
            payload = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
            sig = base64.urlsafe_b64decode(b64sig + "=" * (-len(b64sig) % 4))
        except (ValueError, Exception):
            return False
        expected = hmac.new(self._key, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):          # 关① 印章真伪
            return False
        parts = payload.decode("utf-8").split(":", 2)
        if len(parts) != 3:
            return False
        t_tool, nonce, ts = parts
        if t_tool != tool:                                   # 关② 令牌只给签发的那个工具
            return False
        if nonce in self._nonce_used:                       # 关②b 一次性：用过的 nonce 作废
            return False
        if time.time() - int(ts) > self._token_ttl:          # 关③ 有效期
            return False
        self._nonce_used.add(nonce)
        return True
