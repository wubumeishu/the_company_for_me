/**
 * SettingsPanel.tsx —— ★ Phase 2c 任务1：全局模型配置中心（前端"全局设置"面板）。
 *
 * 数据流：GET /providers（当前配置 + 预设下拉）→ 表单编辑 → PUT /providers 立即写盘
 *         （后端 providers.json 原子落盘，下一个 /run 读新配置，热切换不重启服务）。
 * 预设下拉（本地 8901 池 / Ollama / DeepSeek / OpenAI）= 一键填表；手工修改 = custom。
 * 测试连接 = POST /providers/test（GET {base_url}/models，列出模型数）。
 */
import { useEffect, useState } from "react";

const API = "http://127.0.0.1:8790";

export interface ProviderEntry {
  name: string;
  type: string;
  base_url: string;
  api_key: string;          // 展示态可能是脱敏值（GET 回显）
  default_model: string;
  rate: { windows?: { limit: number; periodSec: number }[]; cooldownSec?: number };
}

interface Preset {
  id: string;
  label: string;
  provider: ProviderEntry;
}

export default function SettingsPanel({ onClose }: { onClose: () => void }) {
  const [entries, setEntries] = useState<ProviderEntry[]>([]);
  const [presets, setPresets] = useState<Preset[]>([]);
  const [sel, setSel] = useState(0);
  const [saved, setSaved] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [testOut, setTestOut] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    fetch(`${API}/providers`)
      .then((r) => r.json())
      .then((d) => {
        setEntries(d.providers?.length ? d.providers : (d.presets?.[0]?.provider ? [d.presets[0].provider] : []));
        setPresets(d.presets ?? []);
        setSel(0);
      })
      .catch((e) => setErr(`加载配置失败（引擎 8790 没起？）: ${e}`));
  }, []);

  const cur = entries[sel];
  const patch = (k: keyof ProviderEntry, v: unknown) =>
    setEntries((ps) => ps.map((p, i) => (i === sel ? { ...p, [k]: v } : p)));

  /** 预设一键填表（切预设 = 重置当前条目为该预设字段） */
  const applyPreset = (pid: string) => {
    const p = presets.find((x) => x.id === pid);
    if (!p) return;
    setEntries((ps) => ps.map((x, i) => (i === sel ? { ...p.provider } : x)));
  };

  const addEntry = () => setEntries((ps) => [
    ...ps,
    { name: `Custom_${ps.length + 1}`, type: "openai-compat",
      base_url: "http://127.0.0.1:8901/v1", api_key: "", default_model: "", rate: {} },
  ]);

  const save = async () => {
    setBusy(true); setErr(null); setSaved(null);
    try {
      const r = await fetch(`${API}/providers`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ providers: entries }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.statusText);
      setSaved(`已保存 ${d.saved?.length ?? entries.length} 条 → providers.json（下一个 run 生效）`);
      // 回显脱敏后的 key（GET 端点已做脱敏）
      const g = await (await fetch(`${API}/providers`)).json();
      setEntries(g.providers ?? []);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  const test = async () => {
    setTesting(true); setTestOut(null);
    try {
      const r = await fetch(`${API}/providers/test`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ providers: [cur] }),
      });
      const d = await r.json();
      const res = d.results?.[0];
      setTestOut(res?.ok
        ? `✓ ${res.name} 连通！${res.count} 个模型可用${res.models?.length ? `（如 ${res.models.slice(0, 3).join(", ")}）` : ""}`
        : `✗ ${res?.name}: ${res?.error ?? "未知错误"}`);
    } catch (e) {
      setTestOut(`测试失败: ${e}`);
    } finally {
      setTesting(false);
    }
  };

  const input: React.CSSProperties = {
    width: "100%", boxSizing: "border-box", padding: "6px 8px", borderRadius: 6,
    border: "1px solid #334155", background: "#0f172a", color: "#e2e8f0",
    fontFamily: "ui-monospace, monospace", fontSize: 12,
  };
  const label: React.CSSProperties = { fontSize: 11, color: "#94a3b8", marginBottom: 3, display: "block" };

  return (
    <div style={{
      position: "absolute", top: 12, right: 12, width: 380, maxHeight: "calc(100% - 24px)",
      overflow: "auto", background: "#0f172a", border: "1px solid #334155",
      borderRadius: 12, padding: 14, zIndex: 20, color: "#e2e8f0",
      fontFamily: "ui-monospace, Consolas, monospace",
      boxShadow: "0 8px 30px rgba(0,0,0,.5)",
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
        <b style={{ fontSize: 13 }}>⚙ 全局模型配置（providers.json）</b>
        <button onClick={onClose} style={{ cursor: "pointer", background: "none", border: "none", color: "#64748b", fontSize: 14 }}>✕</button>
      </div>

      {/* 预设下拉（任务1：本地/云端一键填表） */}
      <label style={label}>预设（一键填表）</label>
      <select style={input} value={presets.some((p) => p.id === "custom") ? "custom" : (presets.find((p) => JSON.stringify(p.provider) === JSON.stringify(cur))?.id ?? "custom")}
              onChange={(e) => e.target.value !== "custom" && applyPreset(e.target.value)}>
        {presets.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
        <option value="custom">手工 / 自定义</option>
      </select>

      {/* 条目切换（多条配置时） */}
      {entries.length > 1 && (
        <div style={{ display: "flex", gap: 4, marginTop: 8, flexWrap: "wrap" }}>
          {entries.map((p, i) => (
            <button key={i} onClick={() => setSel(i)} style={{
              padding: "3px 10px", borderRadius: 999, cursor: "pointer",
              border: `1px solid ${i === sel ? "#38bdf8" : "#334155"}`,
              background: i === sel ? "#1e3a5f" : "transparent",
              color: i === sel ? "#7dd3fc" : "#94a3b8", fontSize: 11,
            }}>{p.name}</button>
          ))}
          <button onClick={addEntry} style={{
            padding: "3px 10px", borderRadius: 999, cursor: "pointer",
            border: "1px dashed #475569", background: "transparent", color: "#64748b", fontSize: 11,
          }}>+ 新增</button>
        </div>
      )}

      {cur && (
        <div style={{ marginTop: 10, display: "grid", gap: 8 }}>
          <div>
            <label style={label}>provider_name</label>
            <input style={input} value={cur.name} onChange={(e) => patch("name", e.target.value)} placeholder="Local_Ollama / Cloud_DeepSeek" />
          </div>
          <div>
            <label style={label}>type（OpenAI 兼容 v1 通吃）</label>
            <select style={input} value={cur.type} onChange={(e) => patch("type", e.target.value)}>
              <option value="openai-compat">openai-compat（OAI/DeepSeek/Ollama/8901池…）</option>
              <option value="ollama">ollama</option>
              <option value="anthropic">anthropic（暂占位，未接）</option>
            </select>
          </div>
          <div>
            <label style={label}>base_url（默认预填本地 8901 池）</label>
            <input style={input} value={cur.base_url} onChange={(e) => patch("base_url", e.target.value)}
                   placeholder="http://127.0.0.1:8901/v1" />
          </div>
          <div>
            <label style={label}>api_key（本地可为空 / 随意填，云端必填）</label>
            <input style={input} type="password" value={cur.api_key} onChange={(e) => patch("api_key", e.target.value)}
                   placeholder="留空 = 本地免密" />
          </div>
          <div>
            <label style={label}>model（如 llama3 / qwen2 / agnes-3.0-flash）</label>
            <input style={input} value={cur.default_model} onChange={(e) => patch("default_model", e.target.value)} />
          </div>
          <div>
            <label style={label}>限流窗口（token 预算/窗口，触底=强制 RESTING）</label>
            <div style={{ display: "flex", gap: 6 }}>
              <input style={{ ...input, width: 90 }} type="number" placeholder="limit"
                     value={cur.rate.windows?.[0]?.limit ?? ""}
                     onChange={(e) => patch("rate", { ...cur.rate, windows: [{ limit: Number(e.target.value) || 0, periodSec: cur.rate.windows?.[0]?.periodSec ?? 3600 }] })} />
              <input style={{ ...input, width: 90 }} type="number" placeholder="periodSec"
                     value={cur.rate.windows?.[0]?.periodSec ?? ""}
                     onChange={(e) => patch("rate", { ...cur.rate, windows: [{ limit: cur.rate.windows?.[0]?.limit ?? 0, periodSec: Number(e.target.value) || 0 }] })} />
              <span style={{ fontSize: 10, color: "#64748b", alignSelf: "center" }}>tok / 秒窗</span>
            </div>
          </div>
        </div>
      )}

      <div style={{ display: "flex", gap: 8, marginTop: 14 }}>
        <button onClick={save} disabled={busy} style={btn("#334155", "#e2e8f0", busy)}>
          {busy ? "保存中…" : "💾 保存设置"}
        </button>
        <button onClick={test} disabled={testing || !cur} style={btn("#1e3a5f", "#7dd3fc", testing)}>
          {testing ? "测试中…" : "🔌 测试连接"}
        </button>
      </div>
      {saved && <div style={{ marginTop: 8, fontSize: 11, color: "#4ade80" }}>{saved}</div>}
      {testOut && <div style={{ marginTop: 8, fontSize: 11, color: testOut.startsWith("✓") ? "#4ade80" : "#f87171" }}>{testOut}</div>}
      {err && <div style={{ marginTop: 8, fontSize: 11, color: "#f87171" }}>{err}</div>}
      <div style={{ marginTop: 10, fontSize: 10, color: "#475569" }}>
        保存 = 立即写入后端 providers.json（与 workflow.json 解耦）；agent.provider 按
        type/name 匹配条目，匹配不到自动降级 mock（shell_log 明示）。
      </div>
    </div>
  );
}

function btn(bg: string, fg: string, disabled: boolean): React.CSSProperties {
  return {
    flex: 1, padding: "7px 10px", borderRadius: 8, cursor: disabled ? "wait" : "pointer",
    border: "none", background: bg, color: fg, fontSize: 12,
    fontFamily: "ui-monospace, monospace", opacity: disabled ? 0.6 : 1,
  };
}
