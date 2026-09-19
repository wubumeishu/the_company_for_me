# Phase 2c · executor 真实 LLM 通道 + 429/503 状态机测试（零网络，transport 注入）
# run: cd backend && python tests/test_llm_executor.py
"""验证任务 3 的状态机稳健性：
  1) LLMClient.chat 注入 transport：429（Retry-After=1）→ ProviderError
     → executor 捕获 → rm.force_rest → agent RESTING + 节点 blocked（≠失败）
  2) 快进时钟越过 resume_at → 重跑 → 200 → 节点 ok + 真实产物
  3) 事件流断言：thinking / rate_limit_hit(429) / usage_settle 全链路证据
"""
import sys, time, json
sys.path.insert(0, '.')
import asyncio
import unittest.mock as mock

from app.engine.events import EventBus
from app.engine.executor import NodeExecutor
from app.engine.resource_manager import ResourceManager, STATE_RESTING, STATE_ACTIVE
from app.engine.provider_registry import (Provider, LLMClient, LLMResult,
                                          ProviderError)
from app.schema.validator import LoadedWorkflow
from app.persistence.progress import ProgressLog
from pathlib import Path

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"
results = []
def check(name, cond, detail=""):
    results.append(cond)
    print(f"  {PASS if cond else FAIL}  {name}  {detail}")

T0 = 5000.0
wf = LoadedWorkflow(raw={
    "meta": {"name": "t", "version": "0"},
    "agents": {"a": {"id": "a", "role": "Dev", "provider": "ollama", "model": "llama3.1:8b",
                      "tools": ["write_file"], "boundaries": {},
                      "outputs": [{"name": "code", "kind": "text"}]}},
    "levels": [{"index": 0, "name": "L0", "nodes": [{"id": "n0", "agent": "a", "task": "写模块"}]}],
    "qa": {"onFailure": ["console"]},
}, workflow_id="t", path=Path("."))

REG = [Provider(name="Local_Ollama", type="openai-compat", base_url="http://x/v1",
                default_model="llama3.1:8b")]

# ---- 假 transport：第 1 拍 429(Retry-After=1)，之后 200 ----
calls = {"n": 0}
def flaky(payload):
    calls["n"] += 1
    if calls["n"] == 1:
        return 429, {"error": {"message": "rate limited"}}, {"Retry-After": "1"}
    return 200, {"choices": [{"message": {"content": "模块完成"}}],
                 "usage": {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80}}, {}

bus = EventBus()
rm = ResourceManager(wf, bus, clock=lambda: T0)
events: list[str] = []
async def capture(ev):
    events.append(ev.type)
bus.subscribe(capture)

node = {"id": "n0", "agent": "a", "task": "写模块"}
ex = NodeExecutor(node, wf.resolved_agent("a"), wf, bus,
                  ProgressLog(Path.cwd()), rm, llm_registry=REG)
ex.level = 0

# 把 client.chat 的 transport 锁死成 flaky（零网络）
real_chat = LLMClient.chat
def pinned_chat(self, system, user, model=None, temperature=0.2, max_tokens=4096, transport=None):
    return real_chat(self, system, user, model=model, temperature=temperature,
                     max_tokens=max_tokens, transport=flaky)

async def main():
    with mock.patch.object(LLMClient, "chat", pinned_chat):
        # ① 429 → blocked + RESTING
        out1 = await ex.run()
        check("429 → 节点挂起 blocked（非失败非通过）", out1.blocked and not out1.ok,
              f"blocked={out1.blocked} ok={out1.ok} err={out1.error and out1.error[:40]}")
        st = rm.states["a"]
        check("agent 被强按 RESTING + reason=llm_http_429",
              st.state == STATE_RESTING and st.reason == "llm_http_429",
              f"state={st.state} reason={st.reason}")
        check("429 恢复时刻 = now + Retry-After(1s)", abs(st.resume_at - (T0 + 1.0)) < 1e-6,
              f"resume_at={st.resume_at}")
        check("事件流含 thinking + rate_limit_hit(429)",
              "thinking" in events and "rate_limit_hit" in events,
              f"{events[:6]}")

        # ② 快进时钟越过恢复时刻 → 重跑（模拟 orchestrator tick 唤醒语义：RESTING 到期自动补判放行）
        rm._now = lambda: T0 + 2.0                    # 快进：now(T0+2) > resume_at(T0+1)
        out2 = await ex.run()
        # acquire 内部：RESTING 到期未唤醒 → 补判放行 → 真调 200
        check("恢复后重跑 → 节点 ok + 真实 LLM 产物", out2.ok and out2.outputs.get("code") == "模块完成",
              f"out={list(out2.outputs)}")
        check("agent 已回 ACTIVE（到期补判路径）", rm.states["a"].state == STATE_ACTIVE,
              f"state={rm.states['a'].state}")
        check("事件流含 usage_settle（真实扣减入账）", "usage_settle" in events,
              f"types={sorted(set(events))}")

    # ③ 无 rm 兜底时 429 → 直接失败（不挂起）
    ex_narm = NodeExecutor(node, wf.resolved_agent("a"), wf, EventBus(),
                           ProgressLog(Path.cwd()), None, llm_registry=REG)
    ex_narm.level = 0
    calls["n"] = 0
    def always_429(payload):
        calls["n"] += 1
        return 429, {"error": {}}, {"Retry-After": "5"}
    with mock.patch.object(LLMClient, "chat",
                           lambda s, u, model=None, **k: real_chat(s, u, model=model, transport=always_429)):
        out3 = await ex_narm.run()
        check("无 rm 时 429 → 节点失败（不挂起，罕见兜底）",
              (not out3.blocked and not out3.ok and "LLM 调用失败" in (out3.error or "")),
              f"err={(out3.error or '')[:40]}")

import asyncio
asyncio.run(main())
print(f"\n{sum(results)}/{len(results)} PASS")
sys.exit(0 if all(results) else 1)
