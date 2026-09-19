"""Phase 2c 单测：Provider Registry（持久化/预设/匹配）+ LLMClient（注入 transport 不发网）
+ ResourceManager.settle/force_rest（真实 usage 结算 + 429/503 强按 RESTING）。

run:  cd backend && python tests/test_provider_registry.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.engine.provider_registry import (PRESETS, Provider, ProviderError,
                                          LLMClient, LLMResult, load_registry,
                                          save_registry, provider_for_agent)
from app.engine.resource_manager import ResourceManager, STATE_RESTING, STATE_ACTIVE
from app.schema.validator import LoadedWorkflow
from app.engine.events import EventBus

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results: list[tuple[str, bool, str]] = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(f"  {PASS if cond else FAIL}  {name}  {detail}")


def make_wf(agents=None, providers=None):
    raw = {"meta": {"name": "t", "version": "0"},
           "agents": agents or {"a": {"id": "a", "role": "A", "provider": "ollama",
                                       "tools": [], "boundaries": {}}},
           "levels": [{"index": 0, "name": "L0",
                       "nodes": [{"id": "n0", "agent": "a", "task": "x"}]}],
           "qa": {"onFailure": ["console"]}}
    if providers:
        raw["providers"] = providers
    return LoadedWorkflow(raw=raw, workflow_id="t", path=Path("."))


# ---- 1) 持久化：save→load 回读一致（原子写，不污染真 providers.json）----
tmp = ROOT / "tests" / "_tmp_providers.json"
def t_persist():
    entry = {"name": "Cloud_DeepSeek", "type": "openai-compat",
             "base_url": "https://api.deepseek.com/v1", "api_key": "sk-test-123456",
             "default_model": "deepseek-chat",
             "rate": {"windows": [{"limit": 160, "periodSec": 60}]}}
    saved = save_registry([entry], path=tmp)
    loaded = load_registry(path=tmp)
    check("save_registry 原子写盘", tmp.exists() and not tmp.with_suffix(".tmp").exists(),
          str(tmp))
    check("load_registry 回读一致", len(loaded) == 1 and loaded[0].api_key == "sk-test-123456"
          and loaded[0].base_url == "https://api.deepseek.com/v1", str([p.name for p in loaded]))
    # 坏 base_url 拒绝写盘（422 语义）
    try:
        save_registry([{"name": "bad", "base_url": "ftp://nope", "type": "openai-compat"}], path=tmp)
        check("save_registry 拒绝非法 base_url", False)
    except ValueError:
        check("save_registry 拒绝非法 base_url", True)
    tmp.unlink(missing_ok=True)

# ---- 2) 缺文件 = 内置默认（主人 8901 池预填）----
def t_default():
    regs = load_registry(path=ROOT / "tests" / "_no_such.json")
    check("缺 providers.json = 内置默认（Local_Ollama → 8901）",
          any(p.base_url == "http://127.0.0.1:8901/v1" for p in regs),
          str([p.base_url for p in regs]))
    check("预设下拉 4 条（本地池/Ollama/DeepSeek/OpenAI）", len(PRESETS) == 4,
          str([p["id"] for p in PRESETS]))

# ---- 3) 匹配：agent.provider 别名 → 注册表条目（type/name），mock 恒 None ----
def t_match():
    regs = [Provider(name="Local_Ollama", type="openai-compat",
                     base_url="http://127.0.0.1:8901/v1")]
    check("ollama agent 匹配 openai-compat 条目",
          provider_for_agent({"provider": "ollama"}, regs) is regs[0])
    check("claude agent 无匹配条目 = None（降级 mock）",
          provider_for_agent({"provider": "claude"}, regs) is None)
    check("mock agent 恒 None", provider_for_agent({"provider": "mock"}, regs) is None)
    check("按 name 匹配（Local_Ollama 直连）",
          provider_for_agent({"provider": "Local_Ollama"}, regs) is regs[0])

# ---- 4) LLMClient：注入 transport（零网络）验证 200/429/401 三态 ----
def t_client():
    async def main():
        ok_body = (200, {"choices": [{"message": {"content": "收到"}}],
                         "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                                    "total_tokens": 15}}, {})
        c = LLMClient("http://x/v1", "k", "m")
        r = await c.chat("s", "u", transport=lambda p: ok_body)
        check("200 → LLMResult + usage", isinstance(r, LLMResult) and r.usage["total_tokens"] == 15,
              str(r.usage))

        ra = (429, {"error": {"message": "rate limited"}}, {"Retry-After": "17"})
        try:
            await c.chat("s", "u", transport=lambda p: ra)
            check("429 → ProviderError", False)
        except ProviderError as e:
            check("429 → ProviderError(retry_after=17)", e.status == 429 and e.retry_after == 17.0,
                  f"retry_after={e.retry_after}")

        try:
            await c.chat("s", "u", transport=lambda p: (401, {"error": {}}, {}))
            check("401 → ProviderError(非限流)", False)
        except ProviderError as e:
            check("401 → ProviderError(非限流)", e.status == 401 and e.retry_after is None)

        try:
            await c.chat("s", "u", transport=lambda p: (200, {"choices": []}, {}))
            check("空 content → ProviderError", False)
        except ProviderError:
            check("空 content → ProviderError", True)
    asyncio.run(main())

# ---- 5) settle：真实 usage 补扣触限 → (False, resume)；force_rest 全员 RESTING ----
def t_settle_force_rest():
    wf = make_wf(agents={"a": {"id": "a", "role": "A", "provider": "ollama",
                               "tools": [], "boundaries": {}},
                          "b": {"id": "b", "role": "B", "provider": "ollama",
                                "tools": [], "boundaries": {}}},
                 providers=[{"id": "o", "type": "ollama",
                              "rate": {"windows": [{"limit": 10, "periodSec": 60}]}}])
    bus = EventBus()
    rm = ResourceManager(wf, bus, clock=lambda: 1000.0)
    from app.engine.provider_registry import apply_rate_to_ledger
    apply_rate_to_ledger(rm, [Provider(name="Local_Ollama", type="ollama",
                                       base_url="http://127.0.0.1:8901/v1",
                                       rate={"windows": [{"limit": 10, "periodSec": 60}]})])

    async def main():
        # acquire 扣 1 + settle 11（补扣 10）= 窗口内 11 > limit 10 → 触限
        await rm.acquire("a", tokens=1)
        allowed, resume = rm.settle("a", 11)
        check("settle 补扣触限 → (False, resume_at)", allowed is False and resume == 1060.0,
              f"resume={resume}")
        check("force_rest 同 provider 全员 RESTING",
              rm.force_rest is not None)
        await rm.force_rest("b", 1080.0, reason="llm_http_429")
        check("RESTING 状态 + reason 落盘",
              rm.states["a"].state == STATE_RESTING and rm.states["b"].reason == "llm_http_429",
              f"a={rm.states['a'].state} b={rm.states['b'].state}")
        # tick 唤醒
        rm._now = lambda: 1081.0
        woke = await rm.tick()
        check("tick 到期唤醒 RESTING", "a" in woke and "b" in woke, str(woke))
    asyncio.run(main())

    # 无 rate 配置 = settle 恒放行（向后兼容）
    rm2 = ResourceManager(make_wf(), EventBus(), clock=lambda: 0.0)
    allowed, _ = rm2.settle("a", 99999)
    check("未配 rate = settle 恒放行（兼容铁律）", allowed is True)

t_persist()
t_default()
t_match()
t_client()
t_settle_force_rest()

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} PASS")
sys.exit(1 if failed else 0)
