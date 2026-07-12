import {
  AlertTriangle,
  Check,
  CircleStop,
  Database,
  KeyRound,
  ListFilter,
  MessageSquare,
  Play,
  RefreshCw,
  Save,
  Send,
  Server,
  Settings as SettingsIcon,
  ShieldCheck,
  Sparkles,
  Trash2,
  X
} from "lucide-react";
import React, { FormEvent, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

type RunStatus =
  | "pending"
  | "running"
  | "waiting_user_input"
  | "waiting_approval"
  | "completed"
  | "failed"
  | "cancelled"
  | "timed_out";

type Run = {
  id: string;
  status: RunStatus;
  goal: string;
  channel: string;
  current_step: number;
  max_steps: number;
  tokens_used: number;
  tool_calls_used: number;
  final_result: Record<string, unknown> | null;
  error: string | null;
  created_at: string;
  updated_at: string;
};

type RunList = { items: Run[]; total: number; limit: number; offset: number };
type Message = { id: string; role: string; content: string; created_at: string };
type Step = { step_no: number; type: string; payload: Record<string, unknown>; token_usage: Record<string, unknown>; created_at: string };
type ToolExecution = { step_no: number; call_index: number; tool_name: string; risk_tier: string; status: string; args: Record<string, unknown>; result: Record<string, unknown> | null; error: string | null };
type RunSteps = { messages: Message[]; steps: Step[]; tool_executions: ToolExecution[] };
type Approval = { id: string; run_id: string; tool_name: string; risk_tier: string; args: Record<string, unknown>; status: string; reason: string | null; requested_at: string };
type RiskTier = "read_only" | "low_risk_write" | "sensitive_write" | "destructive";
type McpServer = { id: string; name: string; base_url: string; enabled: boolean; health_status: string; default_risk_tier: string; last_error: string | null };
type McpTool = { id: string; name: string; description: string; risk_tier: string; enabled: boolean };
type Skill = { id: string; name: string; description: string; status: string; active_revision_id: string | null };
type SkillRevision = { id: string; version: number; status: string; instruction: string; required_tools: string[]; created_at: string };
type LlmProvider = "ollama_cloud" | "openai" | "openrouter";
type LlmSettings = { provider: LlmProvider; base_url: string; model: string; has_api_key: boolean; updated_at: string };
type SlackSettings = { has_bot_token: boolean; has_signing_secret: boolean; webhook_url: string; updated_at: string };
type LlmModels = { models: string[] };

type View = "runs" | "approvals" | "mcp" | "skills" | "settings";

const providerLabels: Record<LlmProvider, string> = {
  ollama_cloud: "Ollama Cloud",
  openai: "OpenAI",
  openrouter: "OpenRouter"
};

const statusOptions: Array<RunStatus | ""> = [
  "",
  "pending",
  "running",
  "waiting_user_input",
  "waiting_approval",
  "completed",
  "failed",
  "cancelled",
  "timed_out"
];

function apiKeyHeaders(apiKey: string): HeadersInit {
  return apiKey ? { "X-API-Key": apiKey, "Content-Type": "application/json" } : { "Content-Type": "application/json" };
}

async function api<T>(path: string, apiKey: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { ...apiKeyHeaders(apiKey), ...(init.headers || {}) }
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      detail = body.detail || detail;
    } catch {
      // keep status text
    }
    throw new Error(detail);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

function fmtDate(value: string): string {
  return new Date(value).toLocaleString();
}

function JsonBlock({ value }: { value: unknown }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}

function StatusPill({ status }: { status: string }) {
  return <span className={`status status-${status}`}>{status.replaceAll("_", " ")}</span>;
}

const viewCopy: Record<View, { title: string; subtitle: string }> = {
  runs: { title: "Runs", subtitle: "Create, poll, inspect, and continue agent runs." },
  approvals: { title: "Approvals", subtitle: "Review sensitive tool calls before execution." },
  mcp: { title: "MCP Servers", subtitle: "Inspect registered MCP domains and tools." },
  skills: { title: "Skills", subtitle: "Review skill registry and active revisions." },
  settings: { title: "Settings", subtitle: "Configure the LLM provider used to run agents." }
};

function App() {
  const [apiKey, setApiKey] = useState(() => localStorage.getItem("harness_api_key") || "");
  const [view, setView] = useState<View>("runs");
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);

  useEffect(() => {
    localStorage.setItem("harness_api_key", apiKey);
  }, [apiKey]);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand"><Sparkles size={20} /> Harness</div>
        <button className={view === "runs" ? "nav active" : "nav"} onClick={() => setView("runs")}><MessageSquare size={18} /> Runs</button>
        <button className={view === "approvals" ? "nav active" : "nav"} onClick={() => setView("approvals")}><ShieldCheck size={18} /> Approvals</button>
        <button className={view === "mcp" ? "nav active" : "nav"} onClick={() => setView("mcp")}><Server size={18} /> MCP</button>
        <button className={view === "skills" ? "nav active" : "nav"} onClick={() => setView("skills")}><Database size={18} /> Skills</button>
        <button className={view === "settings" ? "nav active" : "nav"} onClick={() => setView("settings")}><SettingsIcon size={18} /> Settings</button>
      </aside>
      <main className="workspace">
        <header className="topbar">
          <div>
            <h1>{viewCopy[view].title}</h1>
            <p>{viewCopy[view].subtitle}</p>
          </div>
        </header>
        {apiKey && view === "runs" ? <RunsView apiKey={apiKey} selectedRunId={selectedRunId} onSelectRun={setSelectedRunId} /> : null}
        {apiKey && view === "approvals" ? <ApprovalsView apiKey={apiKey} /> : null}
        {apiKey && view === "mcp" ? <McpView apiKey={apiKey} /> : null}
        {apiKey && view === "skills" ? <SkillsView apiKey={apiKey} /> : null}
        {apiKey && view === "settings" ? <SettingsView apiKey={apiKey} /> : null}
      </main>
      {!apiKey ? <ApiKeyModal onApiKeyChange={setApiKey} /> : null}
    </div>
  );
}

function ApiKeyModal({ onApiKeyChange }: { onApiKeyChange: (key: string) => void }) {
  const [input, setInput] = useState("");
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save(event: FormEvent) {
    event.preventDefault();
    const trimmed = input.trim();
    if (!trimmed) return;
    setChecking(true);
    setError(null);
    try {
      await api<unknown>("/v1/llm-settings", trimmed);
      onApiKeyChange(trimmed);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setChecking(false);
    }
  }

  return (
    <div className="modal-overlay">
      <div className="modal">
        <KeyRound size={28} />
        <h2>API key required</h2>
        <p>Enter your Harness API key to load tenant data and configure agents.</p>
        {error ? <div className="error"><AlertTriangle size={16} /> {error}</div> : null}
        <form className="settings-form" onSubmit={save}>
          <input value={input} onChange={(event) => setInput(event.target.value)} placeholder="hrn_..." type="password" autoFocus />
          <button type="submit" disabled={checking}><Save size={16} /> {checking ? "Checking..." : "Save"}</button>
        </form>
      </div>
    </div>
  );
}

function EmptyState({ text }: { text: string }) {
  return <div className="empty">{text}</div>;
}

function RunsView({ apiKey, selectedRunId, onSelectRun }: { apiKey: string; selectedRunId: string | null; onSelectRun: (id: string | null) => void }) {
  const [status, setStatus] = useState<RunStatus | "">("");
  const [runs, setRuns] = useState<Run[]>([]);
  const [total, setTotal] = useState(0);
  const [goal, setGoal] = useState("");
  const [error, setError] = useState<string | null>(null);
  const selected = useMemo(() => runs.find((run) => run.id === selectedRunId) || null, [runs, selectedRunId]);

  async function loadRuns() {
    const query = status ? `?status=${status}` : "";
    const data = await api<RunList>(`/v1/runs${query}`, apiKey);
    setRuns(data.items);
    setTotal(data.total);
    if (!selectedRunId && data.items[0]) onSelectRun(data.items[0].id);
  }

  useEffect(() => {
    setError(null);
    loadRuns().catch((err) => setError(err.message));
    const timer = window.setInterval(() => loadRuns().catch((err) => setError(err.message)), 3000);
    return () => window.clearInterval(timer);
  }, [apiKey, status]);

  async function createRun(event: FormEvent) {
    event.preventDefault();
    if (!goal.trim()) return;
    try {
      const run = await api<Run>("/v1/runs", apiKey, { method: "POST", body: JSON.stringify({ goal }) });
      setGoal("");
      onSelectRun(run.id);
      await loadRuns();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <section className="split">
      <div className="panel list-panel">
        <form className="create-run" onSubmit={createRun}>
          <textarea value={goal} onChange={(event) => setGoal(event.target.value)} placeholder="Start a new agent run" />
          <button type="submit"><Play size={16} /> Create</button>
        </form>
        <div className="toolbar">
          <label><ListFilter size={16} /><select value={status} onChange={(event) => setStatus(event.target.value as RunStatus | "")}>{statusOptions.map((option) => <option key={option || "all"} value={option}>{option || "all statuses"}</option>)}</select></label>
          <button type="button" onClick={() => loadRuns().catch((err) => setError(err.message))}><RefreshCw size={16} /></button>
        </div>
        {error ? <div className="error"><AlertTriangle size={16} /> {error}</div> : null}
        <div className="muted">{total} runs</div>
        <div className="run-list">
          {runs.map((run) => (
            <button key={run.id} className={run.id === selectedRunId ? "run-row selected" : "run-row"} onClick={() => onSelectRun(run.id)}>
              <span className="run-goal">{run.goal}</span>
              <StatusPill status={run.status} />
              <span className="run-meta">{fmtDate(run.updated_at)}</span>
            </button>
          ))}
        </div>
      </div>
      <RunDetail apiKey={apiKey} run={selected} />
    </section>
  );
}

function RunDetail({ apiKey, run }: { apiKey: string; run: Run | null }) {
  const [detail, setDetail] = useState<RunSteps | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function load() {
    if (!run) return;
    const data = await api<RunSteps>(`/v1/runs/${run.id}/steps`, apiKey);
    setDetail(data);
  }

  useEffect(() => {
    setDetail(null);
    setError(null);
    load().catch((err) => setError(err.message));
    const timer = window.setInterval(() => load().catch((err) => setError(err.message)), 3000);
    return () => window.clearInterval(timer);
  }, [apiKey, run?.id]);

  async function cancelRun() {
    if (!run) return;
    await api<Run>(`/v1/runs/${run.id}/cancel`, apiKey, { method: "POST" });
    await load();
  }

  async function sendMessage(event: FormEvent) {
    event.preventDefault();
    if (!run || !message.trim()) return;
    await api<Run>(`/v1/runs/${run.id}/messages`, apiKey, { method: "POST", body: JSON.stringify({ content: message }) });
    setMessage("");
    await load();
  }

  if (!run) return <div className="panel detail"><EmptyState text="Select a run." /></div>;
  return (
    <div className="panel detail">
      <div className="detail-head">
        <div>
          <h2>{run.goal}</h2>
          <div className="subline"><StatusPill status={run.status} /> <span>{run.tokens_used} tokens</span> <span>{run.tool_calls_used} tool calls</span></div>
        </div>
        <button type="button" onClick={cancelRun} disabled={["completed", "failed", "cancelled", "timed_out"].includes(run.status)}><CircleStop size={16} /> Cancel</button>
      </div>
      {error ? <div className="error"><AlertTriangle size={16} /> {error}</div> : null}
      {run.error ? <div className="error"><AlertTriangle size={16} /> {run.error}</div> : null}
      {run.final_result ? <section className="section"><h3>Final Result</h3><JsonBlock value={run.final_result} /></section> : null}
      <section className="section">
        <h3>Conversation</h3>
        <div className="messages">{(detail?.messages || []).map((msg) => <div key={msg.id} className={`message ${msg.role}`}><b>{msg.role}</b><p>{msg.content}</p></div>)}</div>
        {run.status === "waiting_user_input" ? <form className="reply" onSubmit={sendMessage}><input value={message} onChange={(event) => setMessage(event.target.value)} placeholder="Reply to the agent" /><button type="submit"><Send size={16} /></button></form> : null}
      </section>
      <section className="section">
        <h3>Step Timeline</h3>
        <div className="timeline">{(detail?.steps || []).map((step) => <StepRow key={step.step_no} step={step} executions={(detail?.tool_executions || []).filter((execution) => execution.step_no === step.step_no)} />)}</div>
      </section>
    </div>
  );
}

function StepRow({ step, executions }: { step: Step; executions: ToolExecution[] }) {
  return (
    <details className="step" open={step.step_no >= 0}>
      <summary><span>Step {step.step_no}</span><span>{step.type}</span><span>{fmtDate(step.created_at)}</span></summary>
      <JsonBlock value={step.payload} />
      {executions.map((execution) => <div className="tool-exec" key={`${execution.step_no}-${execution.call_index}`}><div><b>{execution.tool_name}</b> <StatusPill status={execution.status} /> <span>{execution.risk_tier}</span></div><JsonBlock value={{ args: execution.args, result: execution.result, error: execution.error }} /></div>)}
    </details>
  );
}

function ApprovalsView({ apiKey }: { apiKey: string }) {
  const [items, setItems] = useState<Approval[]>([]);
  const [reason, setReason] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);

  async function load() {
    const data = await api<Approval[]>("/v1/approvals?status=pending", apiKey);
    setItems(data);
  }

  useEffect(() => {
    load().catch((err) => setError(err.message));
    const timer = window.setInterval(() => load().catch((err) => setError(err.message)), 3000);
    return () => window.clearInterval(timer);
  }, [apiKey]);

  async function decide(id: string, decision: "approved" | "rejected") {
    await api<Approval>(`/v1/approvals/${id}/decide`, apiKey, { method: "POST", body: JSON.stringify({ decision, reason: reason[id] || null }) });
    await load();
  }

  return <section className="panel full">{error ? <div className="error">{error}</div> : null}<div className="approval-list">{items.length === 0 ? <EmptyState text="No pending approvals." /> : items.map((approval) => <div className="approval" key={approval.id}><div className="approval-head"><div><h3>{approval.tool_name}</h3><p>{approval.risk_tier} · run {approval.run_id.slice(0, 8)}</p></div><StatusPill status={approval.status} /></div><JsonBlock value={approval.args} /><input value={reason[approval.id] || ""} onChange={(event) => setReason({ ...reason, [approval.id]: event.target.value })} placeholder="Decision reason" /><div className="actions"><button onClick={() => decide(approval.id, "approved")}><Check size={16} /> Approve</button><button className="danger" onClick={() => decide(approval.id, "rejected")}><X size={16} /> Reject</button></div></div>)}</div></section>;
}

const riskTierOptions: RiskTier[] = ["read_only", "low_risk_write", "sensitive_write", "destructive"];

function McpView({ apiKey }: { apiKey: string }) {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [toolsByServer, setToolsByServer] = useState<Record<string, McpTool[]>>({});
  const [error, setError] = useState<string | null>(null);
  const [refreshingId, setRefreshingId] = useState<string | null>(null);

  async function load() {
    const data = await api<McpServer[]>("/v1/mcp-servers", apiKey);
    setServers(data);
    const entries = await Promise.all(data.map(async (server) => [server.id, await api<McpTool[]>(`/v1/mcp-servers/${server.id}/tools`, apiKey)] as const));
    setToolsByServer(Object.fromEntries(entries));
  }
  useEffect(() => { load().catch((err) => setError(err.message)); }, [apiKey]);

  async function toggleServer(server: McpServer) {
    await api<McpServer>(`/v1/mcp-servers/${server.id}`, apiKey, { method: "PATCH", body: JSON.stringify({ enabled: !server.enabled }) });
    await load();
  }
  async function toggleTool(serverId: string, tool: McpTool) {
    await api<McpTool>(`/v1/mcp-servers/${serverId}/tools/${tool.id}`, apiKey, { method: "PATCH", body: JSON.stringify({ enabled: !tool.enabled }) });
    await load();
  }
  async function refreshServer(server: McpServer) {
    setError(null);
    setRefreshingId(server.id);
    try {
      await api<{ tool_count: number }>(`/v1/mcp-servers/${server.id}/refresh`, apiKey, { method: "POST" });
      await load();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setRefreshingId(null);
    }
  }

  return (
    <section className="panel full">
      {error ? <div className="error"><AlertTriangle size={16} /> {error}</div> : null}
      <AddServerForm apiKey={apiKey} onAdded={load} />
      <div className="server-list">
        {servers.map((server) => (
          <div className="server-card" key={server.id}>
            <div className="server-head">
              <div><h3>{server.name}</h3><p>{server.base_url}</p></div>
              <div className="actions">
                <button type="button" onClick={() => refreshServer(server)} disabled={refreshingId === server.id}>
                  <RefreshCw size={16} /> {refreshingId === server.id ? "Refreshing..." : "Refresh tools"}
                </button>
                <button type="button" onClick={() => toggleServer(server)}>{server.enabled ? "Disable" : "Enable"}</button>
              </div>
            </div>
            <div className="subline"><StatusPill status={server.health_status} /><span>{server.default_risk_tier}</span></div>
            {server.last_error ? <div className="error"><AlertTriangle size={16} /> {server.last_error}</div> : null}
            <div className="tool-list">
              {(toolsByServer[server.id] || []).length === 0 ? (
                <span className="hint">No tools discovered yet — click "Refresh tools" after registering the server.</span>
              ) : (
                (toolsByServer[server.id] || []).map((tool) => (
                  <label className="tool-toggle" key={tool.id}>
                    <input type="checkbox" checked={tool.enabled} onChange={() => toggleTool(server.id, tool)} />
                    <span><b>{tool.name}</b><small>{tool.description || "No description"}</small></span>
                    <em>{tool.risk_tier}</em>
                  </label>
                ))
              )}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function AddServerForm({ apiKey, onAdded }: { apiKey: string; onAdded: () => Promise<void> }) {
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [authHeaderName, setAuthHeaderName] = useState("");
  const [authHeaderValue, setAuthHeaderValue] = useState("");
  const [riskTier, setRiskTier] = useState<RiskTier>("read_only");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!name.trim() || !baseUrl.trim()) return;
    setSaving(true);
    setError(null);
    try {
      await api<McpServer>("/v1/mcp-servers", apiKey, {
        method: "POST",
        body: JSON.stringify({
          name,
          base_url: baseUrl,
          auth_header_name: authHeaderName || undefined,
          auth_header_value: authHeaderValue || undefined,
          default_risk_tier: riskTier
        })
      });
      setName("");
      setBaseUrl("");
      setAuthHeaderName("");
      setAuthHeaderValue("");
      setRiskTier("read_only");
      await onAdded();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <details className="add-server">
      <summary>Add MCP server</summary>
      {error ? <div className="error"><AlertTriangle size={16} /> {error}</div> : null}
      <form className="settings-form" onSubmit={submit}>
        <label>
          Name
          <input value={name} onChange={(event) => setName(event.target.value)} placeholder="e.g. github" required />
        </label>
        <label>
          Base URL
          <input value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="https://mcp.example.com" required />
        </label>
        <label>
          Auth header name
          <input value={authHeaderName} onChange={(event) => setAuthHeaderName(event.target.value)} placeholder="e.g. Authorization (optional)" />
        </label>
        <label>
          Auth header value
          <input value={authHeaderValue} onChange={(event) => setAuthHeaderValue(event.target.value)} placeholder="Optional" type="password" />
        </label>
        <label>
          Default risk tier
          <select value={riskTier} onChange={(event) => setRiskTier(event.target.value as RiskTier)}>
            {riskTierOptions.map((option) => <option key={option} value={option}>{option}</option>)}
          </select>
        </label>
        <div className="actions">
          <button type="submit" disabled={saving}><Save size={16} /> {saving ? "Adding..." : "Add server"}</button>
        </div>
      </form>
    </details>
  );
}

function SkillsView({ apiKey }: { apiKey: string }) {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [revisions, setRevisions] = useState<Record<string, SkillRevision[]>>({});
  const [error, setError] = useState<string | null>(null);

  async function load() {
    const data = await api<Skill[]>("/v1/skills", apiKey);
    setSkills(data);
    const entries = await Promise.all(data.map(async (skill) => [skill.id, await api<SkillRevision[]>(`/v1/skills/${skill.id}/revisions`, apiKey)] as const));
    setRevisions(Object.fromEntries(entries));
  }
  useEffect(() => { load().catch((err) => setError(err.message)); }, [apiKey]);

  return <section className="panel full">{error ? <div className="error">{error}</div> : null}<div className="skill-grid">{skills.map((skill) => <div className="skill-card" key={skill.id}><div className="skill-head"><div><h3>{skill.name}</h3><p>{skill.description}</p></div><StatusPill status={skill.status} /></div><div className="revision-list">{(revisions[skill.id] || []).map((revision) => <details key={revision.id} className="revision"><summary>v{revision.version} · {revision.status} {skill.active_revision_id === revision.id ? "· active" : ""}</summary><p>{revision.instruction}</p><div className="chips">{revision.required_tools.map((tool) => <span key={tool}>{tool}</span>)}</div></details>)}</div></div>)}</div></section>;
}

const providerDefaultBaseUrls: Record<LlmProvider, string> = {
  ollama_cloud: "https://ollama.com/v1",
  openai: "https://api.openai.com/v1",
  openrouter: "https://openrouter.ai/api/v1"
};

function SettingsView({ apiKey }: { apiKey: string }) {
  return (
    <div className="stack">
      <LlmSettingsView apiKey={apiKey} />
      <SlackSettingsView apiKey={apiKey} />
    </div>
  );
}

function LlmSettingsView({ apiKey }: { apiKey: string }) {
  const [current, setCurrent] = useState<LlmSettings | null>(null);
  const [provider, setProvider] = useState<LlmProvider>("ollama_cloud");
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState(providerDefaultBaseUrls.ollama_cloud);
  const [apiKeyInput, setApiKeyInput] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [models, setModels] = useState<string[]>([]);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const [loadingModels, setLoadingModels] = useState(false);

  async function loadModels(forProvider: LlmProvider, forBaseUrl: string, forApiKey?: string) {
    setModelsError(null);
    setLoadingModels(true);
    try {
      const data = await api<LlmModels>("/v1/llm-settings/models", apiKey, {
        method: "POST",
        body: JSON.stringify({
          provider: forProvider,
          api_key: forApiKey || undefined,
          base_url: forBaseUrl || undefined
        })
      });
      setModels(data.models);
    } catch (err) {
      setModelsError((err as Error).message);
      setModels([]);
    } finally {
      setLoadingModels(false);
    }
  }

  async function load() {
    const data = await api<LlmSettings | null>("/v1/llm-settings", apiKey);
    setCurrent(data);
    if (data) {
      setProvider(data.provider);
      setModel(data.model);
      setBaseUrl(data.base_url);
      loadModels(data.provider, data.base_url);
    }
  }

  useEffect(() => {
    setError(null);
    load().catch((err) => setError(err.message));
  }, [apiKey]);

  function changeProvider(next: LlmProvider) {
    setProvider(next);
    setModels([]);
    setModelsError(null);
    if (!current || baseUrl === providerDefaultBaseUrls[provider]) {
      setBaseUrl(providerDefaultBaseUrls[next]);
    }
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setSaved(false);
    try {
      await api<LlmSettings>("/v1/llm-settings", apiKey, {
        method: "PUT",
        body: JSON.stringify({
          provider,
          model,
          base_url: baseUrl || undefined,
          api_key: apiKeyInput || undefined
        })
      });
      setApiKeyInput("");
      setSaved(true);
      await load();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function remove() {
    setError(null);
    setSaved(false);
    try {
      await api<void>("/v1/llm-settings", apiKey, { method: "DELETE" });
      setCurrent(null);
      setApiKeyInput("");
      await load();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <section className="panel full">
      {error ? <div className="error"><AlertTriangle size={16} /> {error}</div> : null}
      {saved ? <div className="muted">Saved.</div> : null}
      <div className="muted">
        {current
          ? `Currently using ${providerLabels[current.provider]} · ${current.model} · updated ${fmtDate(current.updated_at)}`
          : "No provider configured — runs fall back to the server's default LLM settings."}
      </div>
      <form className="settings-form" onSubmit={save}>
        <label>
          Provider
          <select value={provider} onChange={(event) => changeProvider(event.target.value as LlmProvider)}>
            {(Object.keys(providerLabels) as LlmProvider[]).map((option) => (
              <option key={option} value={option}>{providerLabels[option]}</option>
            ))}
          </select>
        </label>
        <label>
          API key
          <input
            value={apiKeyInput}
            onChange={(event) => setApiKeyInput(event.target.value)}
            onBlur={() => { if (apiKeyInput) loadModels(provider, baseUrl, apiKeyInput); }}
            placeholder={current?.has_api_key ? "Leave blank to keep the current key" : "Required"}
            type="password"
          />
        </label>
        <label>
          Base URL
          <input value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder={providerDefaultBaseUrls[provider]} />
        </label>
        <label>
          Model
          <div className="model-picker">
            {models.length > 0 ? (
              <select value={model} onChange={(event) => setModel(event.target.value)} required>
                {!models.includes(model) && model ? <option value={model}>{model}</option> : null}
                {models.map((option) => (
                  <option key={option} value={option}>{option}</option>
                ))}
              </select>
            ) : (
              <input value={model} onChange={(event) => setModel(event.target.value)} placeholder="e.g. gpt-oss:120b" required />
            )}
            <button
              type="button"
              onClick={() => loadModels(provider, baseUrl, apiKeyInput || undefined)}
              disabled={loadingModels || (!apiKeyInput && !current?.has_api_key)}
              title="Load models"
            >
              <RefreshCw size={16} />
            </button>
          </div>
          {modelsError ? <span className="hint error-hint">Could not load models: {modelsError}</span> : null}
        </label>
        <div className="actions">
          <button type="submit"><Save size={16} /> Save</button>
          {current ? <button type="button" className="danger" onClick={remove}><Trash2 size={16} /> Remove</button> : null}
        </div>
      </form>
    </section>
  );
}

function SlackSettingsView({ apiKey }: { apiKey: string }) {
  const [current, setCurrent] = useState<SlackSettings | null>(null);
  const [botToken, setBotToken] = useState("");
  const [signingSecret, setSigningSecret] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  async function load() {
    const data = await api<SlackSettings | null>("/v1/slack-settings", apiKey);
    setCurrent(data);
  }

  useEffect(() => {
    setError(null);
    load().catch((err) => setError(err.message));
  }, [apiKey]);

  async function save(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setSaved(false);
    try {
      await api<SlackSettings>("/v1/slack-settings", apiKey, {
        method: "PUT",
        body: JSON.stringify({
          bot_token: botToken || undefined,
          signing_secret: signingSecret || undefined
        })
      });
      setBotToken("");
      setSigningSecret("");
      setSaved(true);
      await load();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function remove() {
    setError(null);
    setSaved(false);
    try {
      await api<void>("/v1/slack-settings", apiKey, { method: "DELETE" });
      setCurrent(null);
      setBotToken("");
      setSigningSecret("");
      await load();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <section className="panel full">
      <h2>Slack</h2>
      {error ? <div className="error"><AlertTriangle size={16} /> {error}</div> : null}
      {saved ? <div className="muted">Saved.</div> : null}
      <div className="muted">
        {current
          ? `Connected · updated ${fmtDate(current.updated_at)}`
          : "Not connected — Slack events fall back to the server's default configuration."}
      </div>
      {current ? (
        <div className="muted">
          Event Subscriptions URL: <code>{current.webhook_url}</code>
        </div>
      ) : null}
      <form className="settings-form" onSubmit={save}>
        <label>
          Bot User OAuth Token
          <input
            value={botToken}
            onChange={(event) => setBotToken(event.target.value)}
            placeholder={current?.has_bot_token ? "Leave blank to keep the current token" : "xoxb-..."}
            type="password"
          />
        </label>
        <label>
          Signing Secret
          <input
            value={signingSecret}
            onChange={(event) => setSigningSecret(event.target.value)}
            placeholder={current?.has_signing_secret ? "Leave blank to keep the current secret" : "Required"}
            type="password"
          />
        </label>
        <div className="actions">
          <button type="submit"><Save size={16} /> Save</button>
          {current ? <button type="button" className="danger" onClick={remove}><Trash2 size={16} /> Remove</button> : null}
        </div>
      </form>
    </section>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
