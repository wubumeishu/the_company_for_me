"""
squad.py —— Phase 2 任务 3：敏捷小队（横向抽调）雏形。

机制（对齐 ARCHITECTURE_V2 §1.3 与任务书）：
  kind=squad 的节点（"大型特性开发"）流转到时：
    1) 从 squads 注册表取小队配置（members: 按部门/指定人 + 小队内角色 + toolsOverride）
    2) ResourceManager.deploy_squad() → 成员置 SQUAD_DEPLOYED（原部门任务不再派给它们）
    3) SquadRoom = 局部共享状态（"会议室"）：所有成员读写同一 dict + 发言纪要 transcript
    4) 按角色序发言：pm → ui → dev... → qa（qa 是门禁：验证会议室产物齐全才放行）
    5) 全部通过 → 产物 = room.summary()（含纪要 + 各角色产出）→ 自动解散归建
       （ResourceManager.dissolve_squad + squad_dissolved 事件）；
       qa 不过 → 节点 FAIL（orchestrator 的既有红框/回退三件套照常生效，小队随节点终态收场）

设计铁律（防御性，同 Phase 1）：
  - 成员解析必须有据可查：agent 显式指定 > 部门内按 id 排序取第一个空闲的（确定性，可单测）；
    解析不到 = 节点直接失败（不许悄悄降队），错误进 NodeOutcome；
  - 会议室 State 是纯数据（dict + transcript），不存闭包/活对象 → 可序列化进 checkpoint；
  - 所有小队事件进 EventBus：squad_deployed / squad_speech / squad_qa_pass|fail / squad_dissolved。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .events import EventBus
from .executor import NodeOutcome
from ..persistence.progress import ProgressLog


# --------------------------------------------------------------------------- 会议室
class SquadRoom:
    """小队共享的局部 State（"会议室"）：成员产出的键值 + 发言纪要（带时间戳）。

    纯数据 → 可 json.dumps 进 checkpoint / 事件流（agent-flow-visualization 式透明化）。
    """

    def __init__(self, squad_id: str, task: str):
        self.squad_id = squad_id
        self.task = task
        self.state: dict[str, Any] = {"goal": task}      # 各角色产出按 f"{role}:{key}" 挂进来
        self.transcript: list[dict[str, Any]] = []        # 发言纪要（谁/说啥/何时）
        self.members: list[str] = []                       # 实际入会的 agent id

    def join(self, agent_id: str, role: str) -> None:
        self.members.append(agent_id)
        self._speak(role, agent_id, "入会", None)

    def contribute(self, role: str, agent_id: str, key: str, value: str) -> None:
        """成员把子任务产出写进共享 state（会议室白板）。"""
        self.state[f"{role}:{key}"] = value
        self._speak(role, agent_id, f"产出 {key}", value[:120])

    def _speak(self, role: str, agent_id: str, what: str, detail: Optional[str]) -> None:
        self.transcript.append({
            "t": time.time(), "agent": agent_id, "role": role,
            "what": what, "detail": detail,
        })

    def qa_check(self) -> tuple[bool, list[str]]:
        """QA 门禁（会议室验收）：除 qa 角色外，所有已 join 成员都必须有 contribute 产出。
        返回 (通过?, 缺产出的成员[])。"""
        producers = {e["agent"] for e in self.transcript if e["what"].startswith("产出")}
        qa_agents = {e["agent"] for e in self.transcript if e["role"] == "qa"}
        missing = [m for m in self.members if m not in producers and m not in qa_agents]
        return (len(missing) == 0, missing)

    def summary(self) -> dict[str, Any]:
        """解散时的产物：目标 + 全部白板 + 纪要（回主执行流的交接包）。"""
        return {"goal": self.task, "whiteboard": self.state, "transcript": self.transcript,
                "members": self.members}


# --------------------------------------------------------------------------- 数据
@dataclass
class SquadMember:
    """解析后的小队成员（from 部门/显式 agent → 落到具体 agent_id）。"""
    agent_id: str
    role: str                      # 小队内角色（as：pm/ui/dev/qa…）
    from_dept: Optional[str]
    tools_override: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- 执行器
class SquadExecutor:
    """kind=squad 节点的执行器（orchestrator 分派时调用）。

    Phase 2 雏形：成员"干活"走 mock 通道（与 executor 的 provider 降级一致，
    真 LLM 就位后把 _member_work 换成 NodeExecutor._execute_provider 即可，接口不变）。
    """

    SPEAK_ORDER = ["pm", "ui", "dev", "qa"]   # 发言序：产品→设计→研发→验收

    def __init__(self, wf: "LoadedWorkflow", bus: EventBus, progress: ProgressLog,
                 resource_manager=None):
        from ..schema.validator import LoadedWorkflow
        self.wf = wf
        self.bus = bus
        self.progress = progress
        self.rm = resource_manager

    # ---------------------------------------------------------------- 入口
    async def run(self, node: dict[str, Any], level: int,
                  upstream: dict[str, "NodeOutcome"]) -> NodeOutcome:
        squad_id = node.get("squad")
        squads = self.wf.raw.get("squads", {})
        cfg = squads.get(squad_id) if squad_id else None
        if cfg is None:
            return self._fail(node, level, f"squad '{squad_id}' 未在 squads 注册表定义")

        members = self.resolve_members(cfg)
        if members is None:
            return self._fail(node, level, "小队成员解析失败（部门/agent 不存在）")

        room = SquadRoom(squad_id or "adhoc", node.get("task", ""))
        for m in members:
            room.join(m.agent_id, m.role)

        # ★ 抽调：成员置 SQUAD_DEPLOYED（原部门冻结派活）+ 事件（前端/大盘可见）
        ids = [m.agent_id for m in members]
        if self.rm is not None:
            self.rm.deploy_squad(ids)
        await self.bus.publish("squad_deployed", node_id=node["id"], squad=squad_id,
                               members=[{"agent": m.agent_id, "role": m.role,
                                          "dept": m.from_dept, "tools": m.tools_override}
                                        for m in members],
                               shared_state={"goal": room.task})

        # 执行前打卡（Strict Constraint #2：小队整组也按节点打卡）
        self.progress.node_enter(level, node["id"], "squad:" + str(squad_id),
                                 next_step=f"敏捷小队 {squad_id} 讨论: {node.get('task','')[:40]}…")

        # ---- 会议室讨论：按角色序发言，共享 room.state ----
        for m in sorted(members, key=lambda x: self.SPEAK_ORDER.index(x.role)
                        if x.role in self.SPEAK_ORDER else 99):
            await self._member_work(m, room, node, upstream)

        # ---- QA 门禁（会议室验收）----
        qa_ok, missing = room.qa_check()
        qa_agent = next((m.agent_id for m in members if m.role == "qa"), None)
        if qa_agent:
            room.contribute("qa", qa_agent, "verdict", "PASS" if qa_ok else "REJECT")
        if qa_ok:
            await self.bus.publish("squad_qa_pass", node_id=node["id"], squad=squad_id,
                                   missing=missing)
        else:
            await self.bus.publish("squad_qa_fail", node_id=node["id"], squad=squad_id,
                                   missing=missing)

        # ---- 解散归建（无论成败：节点终态即小队生命周期终点）----
        if self.rm is not None:
            self.rm.dissolve_squad(ids)
        await self.bus.publish("squad_dissolved", node_id=node["id"], squad=squad_id,
                               outcome="pass" if qa_ok else "fail", members=ids)

        self.progress.node_exit(level, node["id"], "squad:" + str(squad_id),
                                completed=f"小队{squad_id}讨论{'通过' if qa_ok else '打回'}")

        if not qa_ok:
            return NodeOutcome(level, node["id"], ok=False,
                              error=f"小队 QA 打回：成员缺产出 {missing}")

        # 产物：会议室交接包（下游节点 inputs 可取 squad_summary）
        summary = room.summary()
        return NodeOutcome(level, node["id"], ok=True,
                           outputs={"squad_summary": str(summary["whiteboard"])[:500],
                                    "squad_transcript": str(len(summary["transcript"]))})

    # ---------------------------------------------------------------- 成员解析
    def resolve_members(self, cfg: dict[str, Any]) -> Optional[list[SquadMember]]:
        """from 部门（或 agent 显式指定）→ 具体 agent。解析失败返回 None。

        部门取人规则（确定性，可单测）：该部门下按 agent_id 排序取第一个；
        agent 显式指定时直接采信（但必须在注册表存在）。
        """
        agents = self.wf.agents
        out: list[SquadMember] = []
        seen: set[str] = set()
        for raw in cfg.get("members", []):
            role = raw.get("as", "dev")
            explicit = raw.get("agent")
            if explicit:
                if explicit not in agents:
                    return None                      # 幽灵引用，拒绝
                aid = explicit
            else:
                dept = raw.get("from")
                if dept is None:
                    return None
                if dept in agents:                  # from 也可能是具体 agent id
                    aid = dept
                else:
                    pool = sorted(a for a, ag in agents.items()
                                  if ag.get("department") == dept)
                    if not pool:
                        return None                 # 该部门无人可抽
                    aid = pool[0]
            if aid in seen:                          # 一人不重复入会
                continue
            seen.add(aid)
            out.append(SquadMember(agent_id=aid, role=role, from_dept=raw.get("from"),
                                   tools_override=raw.get("toolsOverride", [])))
        return out

    # ---------------------------------------------------------------- 成员干活（mock 通道）
    async def _member_work(self, m: SquadMember, room: SquadRoom, node: dict[str, Any],
                           upstream: dict[str, "NodeOutcome"]) -> None:
        agent = self.wf.resolved_agent(m.agent_id)
        prov = agent.get("provider", "mock")

        # 资源门：有 ResourceManager 且 provider 配了限流 → 干活前 acquire（冷却即休息）
        if self.rm is not None and prov != "mock":
            allowed, _resume = await self.rm.acquire(m.agent_id, tokens=64)
            if not allowed:
                room.contribute(m.role, m.agent_id, "blocked", "cooldown 未放行")
                await self.bus.publish("shell_log", node_id=node["id"],
                                       cmd=f"[{m.agent_id}] RESTING，本拍跳过")
                return

        if m.role == "pm":
            # PM：拆解目标（写白板 goal_breakdown）
            room.contribute("pm", m.agent_id, "plan",
                            f"{room.task} → 拆 {len(room.members)} 人分工（会议室共享 state）")
        elif m.role == "qa":
            room.contribute("qa", m.agent_id, "checklist", "对照白板产出清单验收")
        else:
            # dev/ui：产出带工具覆盖标记的代码/设计（文生图工具此时生效：toolsOverride）
            tools = ", ".join(m.tools_override) or "code"
            room.contribute(m.role, m.agent_id, "artifact",
                            f"[{tools}] {room.task[:30]} 的 {m.role} 侧实现")
        await self.bus.publish("squad_speech", node_id=node["id"], agent=m.agent_id,
                               role=m.role, key=list(room.transcript[-1]["what"].split() and ["last"]),
                               state_keys=list(room.state))

    def _fail(self, node: dict[str, Any], level: int, reason: str) -> NodeOutcome:
        return NodeOutcome(level, node["id"], ok=False, error=reason)


# --------------------------------------------------------------------------- 分派助手
def node_is_squad(node: dict[str, Any]) -> bool:
    """orchestrator 分派判断：kind=squad 的节点走 SquadExecutor。"""
    return node.get("kind") == "squad"
