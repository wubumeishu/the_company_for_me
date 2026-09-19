"""
provider_registry.py —— Phase 2c：全局模型配置中心（与 workflow.json 解耦）。

主人指令落地（2026-09-19）：抛弃把端口写死在 workflow.json 里的旧做法——
provider 连接信息（base_url / api_key / model）持久化到仓库根 providers.json，
画布保存=只覆盖 workflow.json（kudosflow 铁律不变），模型配置从此热切换：
每个 /run 重新读注册表，改配置不用改工作流。

构件：
  LLMClient      通用 OpenAI 兼容 chat/completions（v1 接口通吃云端 OAI/DeepSeek/
                 本地 Ollama/主人 8901 QuotaGuard 池）；429/503 带 Retry-After 的
                 ProviderError 抛出 —— executor 捕获后把 Agent 强按进 RESTING。
  provider_for_agent(agent, registry)  解析"这个员工该用哪条 LLM 通道"：
                 provider 别名表匹配注册表条目（按 type 或 name）；
                 匹配到 + 该条目声明了 type=openai-compat/ollama = 真实 LLM 通道；
                 匹配不到 = 降级 mock（executor 打 shell_log 明示，不静默换道）。
  load_registry/save_registry  providers.json 读写（文件缺失 = 返回内置预设默认）；
                 校验 base_url 必须 http(s)，rate.windows 参数合法（坏条目跳过不阻塞）。

铁律：mock 通道行为零变化（注册表里没匹配的 provider 照旧降级 mock）；
api_key 本地模型可为空/随意填（8901 池本地代理不需要真 key）。
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .resource_manager import ResourceManager

REPO_ROOT = Path(__file__).resolve().parents[3]          # backend/app/engine → 仓库根
PROVIDERS_FILE = REPO_ROOT / "providers.json"             # 与 workflow.json 解耦的模型配置中心


# --------------------------------------------------------------------------- 异常
class ProviderError(Exception):
    """真实 LLM 调用失败（executor 消费）。status=None = 网络层错误；
    status=429/503 = 模型端限流（retry_after = Retry-After 头或 None）→ 强按 RESTING。"""
    def __init__(self, message: str, status: Optional[int] = None,
                 retry_after: Optional[float] = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@dataclass
class LLMResult:
    """一次真实 LLM 调用的返回（executor → 资源账本结算用）。"""
    content: str
    usage: dict[str, int]          # {prompt_tokens, completion_tokens, total_tokens}
    provider: str                  # 注册表 name（事件/审计用）
    model: str                     # 实际跑的 model（可能 = 条目 defaultModel）


# --------------------------------------------------------------------------- 客户端
class LLMClient:
    """OpenAI 兼容 chat/completions（stdlib urllib，线程池跑，零新依赖）。

    POST {base_url}/chat/completions —— base_url 预填主人本地 8901 池的 v1 段；
    云端 DeepSeek/OAI/本地 Ollama 同构。api_key 空 = 不发 Authorization（本地免密）。
    """

    def __init__(self, base_url: str, api_key: str = "", model: str = "",
                 timeout: float = 300.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or ""
        self.default_model = model or ""
        self.timeout = timeout

    # ---- 纯函数（可注入 fake_transport → 单测不发网络）----
    def _post(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any], dict[str, str]]:
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, json.loads(resp.read().decode()), {}
        except urllib.error.HTTPError as e:                       # 4xx/5xx 走这里
            body = e.read().decode(errors="replace")[:400]
            return e.code, {"error": {"message": body}}, dict(e.headers)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ProviderError(f"模型端不可达 {self.base_url}: {e}") from e

    @staticmethod
    def _retry_after(headers: dict[str, str]) -> Optional[float]:
        v = headers.get("Retry-After")
        if not v:
            return None
        try:
            return max(0.0, float(v))
        except ValueError:
            return None                                             # 日期格式不解析，保守不给

    async def chat(self, system: str, user: str, model: Optional[str] = None,
                   temperature: float = 0.2, max_tokens: int = 4096,
                   transport: Any = None) -> LLMResult:
        """一次 chat 调用。transport 可注入 (payload)->(status, body, headers) 供单测。"""
        import asyncio
        payload: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        post = transport or self._post
        status, body, headers = await asyncio.to_thread(post, payload)
        if status in (429, 503):
            raise ProviderError(f"模型端限流/过载 HTTP {status}: {str(body.get('error'))[:160]}",
                                status=status, retry_after=self._retry_after(headers))
        if status != 200:
            raise ProviderError(f"LLM 调用失败 HTTP {status}: {str(body)[:200]}", status=status)
        choices = body.get("choices") or []
        content = (choices[0].get("message", {}).get("content", "") if choices else "").strip()
        usage = body.get("usage") or {}
        if not content:
            raise ProviderError(f"模型返回空内容（model={payload['model']}）")
        return LLMResult(content=content,
                         usage={"prompt_tokens": int(usage.get("prompt_tokens") or 0),
                                "completion_tokens": int(usage.get("completion_tokens") or 0),
                                "total_tokens": int(usage.get("total_tokens")
                                                   or int(usage.get("prompt_tokens", 0))
                                                   + int(usage.get("completion_tokens", 0)))},
                         provider=self.default_model or "openai-compat", model=payload["model"])

    async def list_models(self, transport_get: Any = None) -> list[str]:
        """GET {base_url}/models（前端"测试连接"按钮数据源）。"""
        import asyncio

        def _get() -> list[str]:
            req = urllib.request.Request(self.base_url + "/models",
                                         headers={"Authorization": f"Bearer {self.api_key}"}
                                         if self.api_key else {})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            return [m.get("id", "") for m in data.get("data", [])]

        fetch = transport_get or _get
        try:
            return await asyncio.to_thread(fetch)
        except Exception as e:
            raise ProviderError(f"模型端不可达 {self.base_url}: {e}") from e


# --------------------------------------------------------------------------- 解析
# agent.provider 词汇 → 注册表条目匹配词（先匹配 type 再匹配 name，同 rm.PROVIDER_ALIAS）
PROVIDER_ALIASES = {
    "ollama": ("ollama", "openai-compat"),
    "claude": ("claude", "anthropic"),
    "codex": ("codex", "codex-cli"),
    "mock": ("mock",),
    "builtin": ("builtin",),
    "openai-compat": ("openai-compat", "ollama"),
}

LLM_CAPABLE_TYPES = {"openai-compat", "ollama"}          # 这两类条目 = 真实 LLM 通道

NAME_RE = re.compile(r"^[\w.-]{2,40}$")


@dataclass
class Provider:
    name: str                 # provider_name（Local_Ollama / Cloud_DeepSeek …）
    type: str = "openai-compat"
    base_url: str = ""
    api_key: str = ""
    default_model: str = ""
    rate: dict[str, Any] = None                    # type: ignore[valid-type]

    def __post_init__(self) -> None:
        if self.rate is None:
            self.rate = {}

    def to_dict(self, mask_key: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "type": self.type, "base_url": self.base_url,
                              "api_key": _mask(self.api_key) if mask_key else self.api_key,
                              "default_model": self.default_model, "rate": self.rate}
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Provider":
        return cls(name=str(d["name"]), type=str(d.get("type", "openai-compat")),
                   base_url=str(d.get("base_url", "")).rstrip("/"),
                   api_key=str(d.get("api_key", "")),
                   default_model=str(d.get("default_model", "")),
                   rate=d.get("rate") or {})

    def client(self, model: Optional[str] = None) -> LLMClient:
        return LLMClient(self.base_url, self.api_key, model or self.default_model)


def _mask(key: str) -> str:
    """GET 端点脱敏：>8 位显示前 4 + 后 4；空 = '未设置'。"""
    if not key:
        return ""
    if len(key) <= 8:
        return key[:2] + "****"
    return f"{key[:4]}****{key[-4:]}"


def provider_for_agent(agent: dict[str, Any], registry: list[Provider]) -> Optional[Provider]:
    """按 agent.provider 别名匹配注册表（type 或 name）；只认 LLM 可用类型。"""
    prov = str((agent or {}).get("provider", "mock"))
    if prov in ("mock", "builtin"):
        return None
    for alias in PROVIDER_ALIASES.get(prov, (prov,)):
        for p in registry:
            if p.type == alias or p.name.lower() == alias.lower():
                if p.type in LLM_CAPABLE_TYPES:
                    return p
    return None


# --------------------------------------------------------------------------- 预设
# 前端设置面板下拉（本地/云端一键填表；手工改字段 = custom 模式）
PRESETS: list[dict[str, Any]] = [
    {"id": "local_quota_guard", "label": "本地 · QuotaGuard 池 8901（默认预填）",
     "provider": {"name": "Local_Ollama", "type": "openai-compat",
                  "base_url": "http://127.0.0.1:8901/v1", "api_key": "",
                  "default_model": "agnes-3.0-flash",
                  "rate": {"windows": [{"limit": 1500, "periodSec": 18000}],
                            "cooldownSec": 30}}},
    {"id": "local_ollama", "label": "本地 · Ollama 11434",
     "provider": {"name": "Local_Ollama", "type": "ollama",
                  "base_url": "http://127.0.0.1:11434/v1", "api_key": "ollama",
                  "default_model": "llama3.1:8b", "rate": {}}},
    {"id": "cloud_deepseek", "label": "云端 · DeepSeek",
     "provider": {"name": "Cloud_DeepSeek", "type": "openai-compat",
                  "base_url": "https://api.deepseek.com/v1", "api_key": "",
                  "default_model": "deepseek-chat",
                  "rate": {"windows": [{"limit": 600000, "periodSec": 3600}],
                            "cooldownSec": 30}}},
    {"id": "cloud_openai", "label": "云端 · OpenAI",
     "provider": {"name": "Cloud_OpenAI", "type": "openai-compat",
                  "base_url": "https://api.openai.com/v1", "api_key": "",
                  "default_model": "gpt-4o-mini",
                  "rate": {"windows": [{"limit": 100000, "periodSec": 3600}],
                            "cooldownSec": 30}}},
]

DEFAULT_REGISTRY: list[dict[str, Any]] = [PRESETS[0]["provider"]]     # 缺省 = 主人本地池


# --------------------------------------------------------------------------- 持久化
def _valid_windows(rate: dict[str, Any]) -> bool:
    ws = rate.get("windows") or []
    if not ws:
        return True
    for w in ws:
        if not (int(w.get("limit", 0)) >= 1 and int(w.get("periodSec", 0)) >= 1):
            return False
    return True


def load_registry(path: Optional[Path] = None) -> list[Provider]:
    """读 providers.json（缺失/坏文件 = 内置默认 + 打 console 提示，绝不阻塞 run）。"""
    p = path or PROVIDERS_FILE
    if not p.exists():
        return [Provider.from_dict(d) for d in DEFAULT_REGISTRY]
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        entries = raw.get("providers", raw if isinstance(raw, list) else [])
    except (json.JSONDecodeError, OSError) as e:
        print(f"[providers] 配置解析失败({e}) → 用内置预设", flush=True)
        return [Provider.from_dict(d) for d in DEFAULT_REGISTRY]
    out: list[Provider] = []
    for d in entries:
        try:
            prov = Provider.from_dict(d)
        except (KeyError, TypeError, ValueError):
            print(f"[providers] 跳过坏条目（缺 name）: {str(d)[:120]}", flush=True)
            continue
        if not prov.base_url.startswith(("http://", "https://")):
            print(f"[providers] 跳过 {prov.name}: base_url 非法 {prov.base_url}", flush=True)
            continue
        if not NAME_RE.match(prov.name):
            print(f"[providers] 跳过 {prov.name}: name 非法（2-40 位 \\w.-）", flush=True)
            continue
        if not _valid_windows(prov.rate):
            print(f"[providers] {prov.name} rate.windows 参数非法（limit/periodSec 需 ≥1）", flush=True)
        out.append(prov)
    return out or [Provider.from_dict(d) for d in DEFAULT_REGISTRY]


def save_registry(providers: list[dict[str, Any]], path: Optional[Path] = None) -> list[Provider]:
    """前端"保存设置"写盘：校验后原子写（tmp+rename 防半截文件）；坏条目 422 不写盘。"""
    p = path or PROVIDERS_FILE
    clean: list[Provider] = []
    for d in providers:
        prov = Provider.from_dict(d)                        # 缺 name/坏 base_url → 抛 ValueError
        if not prov.base_url.startswith(("http://", "https://")):
            raise ValueError(f"provider '{prov.name}' base_url 必须 http(s):// 开头")
        clean.append(prov)
    text = json.dumps({"version": 1, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
                       "providers": [c.to_dict() for c in clean]},
                      ensure_ascii=False, indent=2)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)                                          # 原子落盘
    return clean


def apply_rate_to_ledger(rm: ResourceManager, registry: list[Provider]) -> None:
    """把注册表的 rate.windows 灌进对应 provider 的 RateLedger（引擎按注册表限流，
    不依赖 workflow.json 的 providers[] 死配置——旧字段保留但注册表优先）。"""
    by_prov: dict[str, list[str]] = {}
    for aid, agent in rm.wf.agents.items():
        by_prov.setdefault(str(agent.get("provider", "mock")), []).append(aid)
    for name, aids in by_prov.items():
        entry = next((p for p in registry
                      if p.type in LLM_CAPABLE_TYPES and
                      (p.type in PROVIDER_ALIASES.get(name, (name,)) or
                       p.name.lower() in PROVIDER_ALIASES.get(name, (name,)))), None)
        if entry is None or not entry.rate.get("windows"):
            continue
        from .resource_manager import RateLedger, WindowSpec
        rm.ledgers[name] = RateLedger(
            [WindowSpec(int(w["limit"]), int(w["periodSec"]), int(w.get("cooldownSec", 0)))
             for w in entry.rate["windows"]],
            int(entry.rate.get("cooldownSec", 0)))
        # agent 共享 provider 级账本（限流是 provider 事实，非个人配额）→ 重写 ledger 即全员生效
