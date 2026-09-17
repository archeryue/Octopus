import { useCallback, useEffect, useState } from "react";
import { IconArchive, IconPlus, IconTrash } from "@tabler/icons-react";
import { fetchAgentConnectors, toggleAgentConnector } from "../api/connectors";
import {
  useSessionStore,
  type Agent,
  type SubagentDefinition,
} from "../stores/sessionStore";
import { PageHeader } from "./PageHeader";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Label } from "./ui/label";

const API = `${window.location.origin}/api/agents`;

/** Built-in MCP servers every agent gets unless told otherwise. */
const BUILTIN_MCP = ["ask", "bg", "ask_agent", "research"] as const;

type Tab = "archived" | "create";

/** The agent form — "Agents › New Agent" / "Agents › <name>" as a full page.
 *
 * Replaces the two-pane settings dialog. Same fields, laid out the way the
 * console design lays them out: identity across the top, the system prompt as
 * the wide centrepiece, then engine / connectors / tool policy in a row of
 * equal-weight panels. The **Archived** tab is where archived agents come
 * back from — the design's market slot, filled with your own shelved agents.
 */
export function AgentFormPage({
  onToggleSidebar,
}: {
  onToggleSidebar: () => void;
}) {
  const token = useSessionStore((s) => s.token);
  const agents = useSessionStore((s) => s.agents);
  const upsertAgent = useSessionStore((s) => s.upsertAgent);
  const removeAgent = useSessionStore((s) => s.removeAgent);
  const editingAgentId = useSessionStore((s) => s.editingAgentId);
  const credentials = useSessionStore((s) => s.credentials);
  const installations = useSessionStore((s) => s.connectorInstallations);
  const availableBackends = useSessionStore((s) => s.availableBackends);
  const showChat = useSessionStore((s) => s.showChat);
  const openAgentForm = useSessionStore((s) => s.openAgentForm);
  const setActiveAgentId = useSessionStore((s) => s.setActiveAgentId);

  const editing = agents.find((a) => a.id === editingAgentId) ?? null;

  const [tab, setTab] = useState<Tab>("create");
  const [archived, setArchived] = useState<Agent[]>([]);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [avatar, setAvatar] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [backend, setBackend] = useState("claude-code");
  const [credentialId, setCredentialId] = useState("");
  const [toolAllow, setToolAllow] = useState("");
  const [toolDeny, setToolDeny] = useState("");
  const [enabledConnectors, setEnabledConnectors] = useState<string[]>([]);
  // Sub-agents this agent brings with it (native-subagents.md §6). Empty is
  // the normal case: the CLI's own built-ins are always available.
  const [subagents, setSubagents] = useState<SubagentDefinition[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const headers = {
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
  };

  // Seed the form from whichever agent we're editing (or blank for a draft).
  // Keyed on the id so switching agents from the sidebar refills it.
  useEffect(() => {
    setName(editing?.name ?? "");
    setDescription(editing?.description ?? "");
    setAvatar(editing?.avatar ?? "");
    setSystemPrompt(editing?.system_prompt ?? "");
    setBackend(editing?.backend ?? "claude-code");
    setCredentialId(editing?.credential_id ?? "");
    setToolAllow(editing?.tool_allow ?? "");
    setToolDeny(editing?.tool_deny ?? "");
    setSubagents(editing?.subagents ?? []);
    setError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editingAgentId]);

  useEffect(() => {
    if (!editing) {
      setEnabledConnectors([]);
      return;
    }
    fetchAgentConnectors(token, editing.id)
      .then(setEnabledConnectors)
      .catch(() => setEnabledConnectors([]));
  }, [token, editing]);

  const loadArchived = useCallback(async () => {
    try {
      const res = await fetch(`${API}?include_archived=true`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) return;
      const all = (await res.json()) as Agent[];
      setArchived(all.filter((a) => a.archived));
    } catch {
      // empty tab; the create flow is unaffected
    }
  }, [token]);

  useEffect(() => {
    loadArchived();
  }, [loadArchived]);

  const save = async () => {
    if (!name.trim() || busy) return;
    setBusy(true);
    setError(null);
    const body = {
      name: name.trim(),
      description: description.trim(),
      avatar: avatar.trim() || null,
      system_prompt: systemPrompt,
      credential_id: credentialId || null,
      backend,
      tool_allow: toolAllow,
      tool_deny: toolDeny,
      // Unnamed rows are drafts the user never filled in, not definitions.
      subagents: subagents.filter((sa) => sa.name.trim()),
      ...(editing ? {} : { mcp_servers: [...BUILTIN_MCP] }),
    };
    try {
      const res = await fetch(editing ? `${API}/${editing.id}` : API, {
        method: editing ? "PATCH" : "POST",
        headers,
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => null);
        throw new Error(detail?.detail || `HTTP ${res.status}`);
      }
      const saved = (await res.json()) as Agent;
      upsertAgent(saved);
      // Select what you just made: the sidebar unfolds it, a new session
      // lands under it, and the account menu's "Agent settings" edits it.
      setActiveAgentId(saved.id);
      showChat();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save agent");
    } finally {
      setBusy(false);
    }
  };

  const archive = async () => {
    if (!editing || editing.is_system) return;
    if (!window.confirm(`Archive "${editing.name}"? You can restore it later.`))
      return;
    await fetch(`${API}/${editing.id}/archive`, { method: "POST", headers });
    removeAgent(editing.id);
    loadArchived();
    showChat();
  };

  const restore = async (agent: Agent) => {
    const res = await fetch(`${API}/${agent.id}/unarchive`, {
      method: "POST",
      headers,
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => null);
      setError(detail?.detail || "Failed to restore agent");
      return;
    }
    const restored = (await res.json()) as Agent;
    upsertAgent(restored);
    setArchived((cur) => cur.filter((a) => a.id !== agent.id));
    openAgentForm(restored.id);
    setTab("create");
  };

  const toggleConnector = async (installationId: string, enabled: boolean) => {
    if (!editing) return;
    setEnabledConnectors((cur) =>
      enabled ? [...cur, installationId] : cur.filter((i) => i !== installationId)
    );
    try {
      await toggleAgentConnector(token, editing.id, installationId, enabled);
    } catch {
      setEnabledConnectors((cur) =>
        enabled ? cur.filter((i) => i !== installationId) : [...cur, installationId]
      );
    }
  };

  const backendCreds = credentials.filter((c) => c.backend === backend);

  return (
    <div className="agent-form-page agent-settings flex min-h-0 flex-1 flex-col">
      <PageHeader
        crumbs={["Agents", editing ? editing.name : "New Agent"]}
        onToggleSidebar={onToggleSidebar}
        actions={
          <>
            <TabSwitch tab={tab} setTab={setTab} archivedCount={archived.length} />
            {tab === "create" && (
              <>
                {editing && !editing.is_system && (
                  <button
                    type="button"
                    className="btn-agent-archive inline-flex items-center gap-1.5 text-[13.5px] text-gray-800 transition-colors hover:text-warn-foreground"
                    onClick={archive}
                  >
                    <IconArchive size={15} />
                    Archive agent
                  </button>
                )}
                <button
                  type="button"
                  className="btn-agent-cancel text-[13.5px] text-gray-800 transition-colors hover:text-gray-950"
                  onClick={showChat}
                >
                  Cancel
                </button>
                <Button
                  className="btn-agent-save"
                  size="sm"
                  onClick={save}
                  disabled={busy || !name.trim()}
                >
                  {editing ? "Save Agent" : "Create Agent"}
                </Button>
              </>
            )}
          </>
        }
      />

      <div className="page-body">
        <div className="mx-auto w-full max-w-4xl">
          {error && (
            <div className="agent-form-error mb-4 rounded-lg border border-danger-border bg-danger-bg px-3.5 py-2.5 text-[13px] text-destructive">
              {error}
            </div>
          )}

          {tab === "archived" ? (
            <ArchivedAgents items={archived} onRestore={restore} />
          ) : (
            <div className="space-y-6">
              <div className="flex items-start gap-4">
                <span className="tile mt-7 size-14 rounded-xl bg-primary text-2xl text-white">
                  {avatar || (name.trim()[0] || "A").toUpperCase()}
                </span>
                <div className="grid flex-1 gap-4 md:grid-cols-2">
                  <div className="space-y-2">
                    <Label htmlFor="agent-name">Name</Label>
                    <div className="flex gap-2">
                      <Input
                        id="agent-avatar"
                        className="agent-avatar-input w-16 text-center"
                        value={avatar}
                        onChange={(e) => setAvatar(e.target.value)}
                        placeholder="🐙"
                        maxLength={4}
                        aria-label="Avatar"
                      />
                      <Input
                        id="agent-name"
                        className="agent-name-input flex-1"
                        value={name}
                        onChange={(e) => setName(e.target.value)}
                        placeholder="sre-ops"
                        disabled={!!editing?.is_system}
                        autoFocus={!editing}
                      />
                    </div>
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="agent-role">One-line role</Label>
                    <Input
                      id="agent-role"
                      className="agent-role-input"
                      value={description}
                      onChange={(e) => setDescription(e.target.value)}
                      placeholder="Production stability triage & release checkup"
                    />
                  </div>
                </div>
              </div>

              <div className="space-y-2">
                <Label htmlFor="agent-prompt">
                  System prompt
                  <span className="ml-2 font-mono text-[11px] font-normal text-gray-700">
                    system_prompt
                  </span>
                </Label>
                <textarea
                  id="agent-prompt"
                  className="agent-prompt-input min-h-32 w-full rounded-xl border border-gray-400 bg-card px-4 py-3 text-[13.5px] leading-relaxed text-gray-900 outline-none transition-colors placeholder:text-gray-600 focus:border-primary focus:ring-[3px] focus:ring-primary/10"
                  value={systemPrompt}
                  onChange={(e) => setSystemPrompt(e.target.value)}
                  placeholder="You are a senior SRE. Pull evidence via connectors first, then give an initial read with actionable advice. All write operations request approval first — humans make the final call."
                />
              </div>

              <div className="grid gap-6 md:grid-cols-2">
                <div className="space-y-2">
                  <Label>
                    Engine
                    <span className="ml-2 font-normal text-gray-700">
                      overridable per session
                    </span>
                  </Label>
                  <div className="agent-backend-select grid grid-cols-2 gap-3" role="radiogroup">
                    {["claude-code", "codex"].map((b) => {
                      const picked = backend === b;
                      const usable = availableBackends.includes(b);
                      return (
                        <button
                          key={b}
                          type="button"
                          className={`agent-backend-option btn-agent-backend-${b} rounded-xl border px-4 py-3 text-left transition-colors ${
                            picked
                              ? "border-primary-200 bg-primary-50"
                              : "border-gray-400 hover:bg-gray-50"
                          } ${usable ? "" : "opacity-50"}`}
                          onClick={() => {
                            setBackend(b);
                            setCredentialId("");
                          }}
                          role="radio"
                          aria-checked={picked}
                          title={usable ? undefined : `${b} is not installed here`}
                        >
                          <span className="flex items-center gap-2">
                            <span
                              className={`inline-block size-1.5 rounded-full ${
                                usable ? "bg-success" : "bg-gray-500"
                              }`}
                            />
                            <span
                              className={`font-mono text-[12.5px] ${
                                picked ? "font-semibold text-primary" : "text-gray-900"
                              }`}
                            >
                              {b === "claude-code" ? "Claude Code" : "Codex"}
                            </span>
                          </span>
                        </button>
                      );
                    })}
                  </div>
                  <select
                    id="agent-credential"
                    className="agent-credential-select mt-1 h-9 w-full rounded-lg border border-gray-400 bg-card px-3 text-[13.5px] text-gray-900 outline-none transition-colors focus:border-primary focus:ring-[3px] focus:ring-primary/10"
                    value={credentialId}
                    onChange={(e) => setCredentialId(e.target.value)}
                    aria-label="Credential"
                  >
                    <option value="">Host default sign-in</option>
                    {backendCreds.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.label}
                      </option>
                    ))}
                  </select>
                  <p className="font-mono text-[11px] text-gray-700">
                    from Harness · memory lives on the agent, survives engine
                    swaps
                  </p>
                </div>

                <div className="space-y-2">
                  <Label>
                    Connectors
                    {!editing && (
                      <span className="ml-2 font-normal text-gray-700">
                        enable after creating
                      </span>
                    )}
                  </Label>
                  <div className="agent-connectors card divide-y divide-gray-300">
                    {installations.map((inst) => {
                      const on = enabledConnectors.includes(inst.id);
                      return (
                        <label
                          key={inst.id}
                          className={`flex cursor-pointer items-center gap-3 px-4 py-3 ${
                            editing ? "" : "cursor-not-allowed opacity-60"
                          }`}
                        >
                          <input
                            type="checkbox"
                            className="size-4 accent-[hsl(var(--primary-600))]"
                            checked={on}
                            disabled={!editing}
                            onChange={(e) =>
                              toggleConnector(inst.id, e.target.checked)
                            }
                          />
                          <span className="truncate text-[13.5px] text-gray-900">
                            {inst.label}
                          </span>
                          <span className="ml-auto font-mono text-[11px] text-gray-700">
                            {inst.kind}
                          </span>
                        </label>
                      );
                    })}
                    {installations.length === 0 && (
                      <p className="px-4 py-3 text-[13px] text-gray-700">
                        No connectors installed yet.
                      </p>
                    )}
                  </div>
                </div>
              </div>

              <div className="grid gap-6 md:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="agent-tool-allow">
                    Tool policy
                    <span className="ml-2 font-mono text-[11px] font-normal text-gray-700">
                      tool_policy
                    </span>
                  </Label>
                  <textarea
                    id="agent-tool-allow"
                    className="agent-tool-allow min-h-20 w-full rounded-xl border border-gray-400 bg-card px-4 py-3 font-mono text-[12.5px] text-gray-900 outline-none transition-colors placeholder:text-gray-600 focus:border-primary focus:ring-[3px] focus:ring-primary/10"
                    value={toolAllow}
                    onChange={(e) => setToolAllow(e.target.value)}
                    placeholder="allow (one per line) — empty allows everything"
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="agent-tool-deny">
                    Denied tools
                    <span className="ml-2 font-normal text-gray-700">
                      deny wins over allow
                    </span>
                  </Label>
                  <textarea
                    id="agent-tool-deny"
                    className="agent-tool-deny min-h-20 w-full rounded-xl border border-gray-400 bg-card px-4 py-3 font-mono text-[12.5px] text-gray-900 outline-none transition-colors placeholder:text-gray-600 focus:border-primary focus:ring-[3px] focus:ring-primary/10"
                    value={toolDeny}
                    onChange={(e) => setToolDeny(e.target.value)}
                    placeholder="deny (one per line)"
                  />
                </div>
              </div>

              <SubagentEditor value={subagents} onChange={setSubagents} />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/** Sub-agents an agent brings with it (native-subagents.md §6).
 *
 * These are handed to the CLI as session-scoped definitions, so they *add* to
 * the built-in ones (Explore, Plan, general-purpose) rather than replacing
 * them — which is why an empty list is the normal, fully-functional state and
 * the section says so rather than looking unfinished.
 */
function SubagentEditor({
  value,
  onChange,
}: {
  value: SubagentDefinition[];
  onChange: (next: SubagentDefinition[]) => void;
}) {
  const update = (i: number, patch: Partial<SubagentDefinition>) =>
    onChange(value.map((sa, n) => (n === i ? { ...sa, ...patch } : sa)));

  return (
    <div className="agent-subagents space-y-3">
      <div className="flex items-center justify-between">
        <Label>
          Sub-agents
          <span className="ml-2 font-normal text-gray-700">
            extra helpers this agent can hand work to, on top of the built-in
            ones
          </span>
        </Label>
        <button
          type="button"
          className="btn-subagent-add inline-flex items-center gap-1.5 rounded-lg border border-gray-400 px-2.5 py-1 text-[12.5px] text-gray-900 transition-colors hover:border-primary hover:text-primary"
          onClick={() =>
            onChange([...value, { name: "", description: "", prompt: "", model: null, tools: [] }])
          }
        >
          <IconPlus size={14} />
          Add
        </button>
      </div>

      {value.length === 0 ? (
        <p className="subagent-empty text-[12.5px] text-gray-700">
          None — this agent uses the harness's built-in sub-agents.
        </p>
      ) : (
        <div className="space-y-2.5">
          {value.map((sa, i) => (
            <div
              key={i}
              className="subagent-row space-y-2 rounded-xl border border-gray-400 bg-gray-50 p-3"
            >
              <div className="flex items-center gap-2">
                <input
                  className="subagent-name flex-1 rounded-lg border border-gray-400 bg-card px-3 py-1.5 font-mono text-[12.5px] text-gray-900 outline-none focus:border-primary"
                  value={sa.name}
                  onChange={(e) => update(i, { name: e.target.value })}
                  placeholder="name — how the model addresses it (e.g. reviewer)"
                />
                <button
                  type="button"
                  className="btn-subagent-remove inline-flex size-8 items-center justify-center rounded-lg text-gray-600 transition-colors hover:bg-danger-bg hover:text-destructive"
                  onClick={() => onChange(value.filter((_, n) => n !== i))}
                  aria-label={`Remove ${sa.name || "sub-agent"}`}
                >
                  <IconTrash size={14} />
                </button>
              </div>
              <input
                className="subagent-description w-full rounded-lg border border-gray-400 bg-card px-3 py-1.5 text-[12.5px] text-gray-900 outline-none focus:border-primary"
                value={sa.description ?? ""}
                onChange={(e) => update(i, { description: e.target.value })}
                placeholder="when to use it — the model reads this to decide"
              />
              <textarea
                className="subagent-prompt min-h-16 w-full rounded-lg border border-gray-400 bg-card px-3 py-2 text-[12.5px] text-gray-900 outline-none focus:border-primary"
                value={sa.prompt ?? ""}
                onChange={(e) => update(i, { prompt: e.target.value })}
                placeholder="its own system prompt"
              />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function TabSwitch({
  tab,
  setTab,
  archivedCount,
}: {
  tab: Tab;
  setTab: (t: Tab) => void;
  archivedCount: number;
}) {
  return (
    <div className="form-tabs flex items-center rounded-lg border border-gray-400 bg-gray-50 p-0.5">
      <button
        type="button"
        className={`btn-tab-archived rounded-md px-3 py-1 text-[13px] transition-colors ${
          tab === "archived"
            ? "bg-card font-semibold text-gray-950 shadow-sm"
            : "text-gray-800"
        }`}
        onClick={() => setTab("archived")}
      >
        Archived{archivedCount > 0 ? ` ${archivedCount}` : ""}
      </button>
      <button
        type="button"
        className={`btn-tab-create rounded-md px-3 py-1 text-[13px] transition-colors ${
          tab === "create"
            ? "bg-card font-semibold text-gray-950 shadow-sm"
            : "text-gray-800"
        }`}
        onClick={() => setTab("create")}
      >
        {"Create"}
      </button>
    </div>
  );
}

function ArchivedAgents({
  items,
  onRestore,
}: {
  items: Agent[];
  onRestore: (agent: Agent) => void;
}) {
  if (items.length === 0) {
    return (
      <div className="archived-empty card px-6 py-12 text-center">
        <IconArchive size={22} className="mx-auto mb-3 text-gray-600" />
        <p className="text-sm text-gray-900">No archived agents.</p>
        <p className="mt-1.5 text-[13px] text-gray-700">
          Archiving an agent keeps its prompt, memory and history — restoring
          brings all of it back.
        </p>
      </div>
    );
  }
  return (
    <div className="archived-grid grid gap-4 md:grid-cols-2 xl:grid-cols-3">
      {items.map((agent) => (
        <div
          key={agent.id}
          className="archived-item card flex flex-col px-5 py-4"
        >
          <div className="flex items-center gap-2.5">
            <span className="tile tile-lg tile-plain">{agent.avatar || "🐙"}</span>
            <span className="truncate text-[15px] font-semibold text-gray-950">
              {agent.name}
            </span>
          </div>
          <p className="mt-3 line-clamp-4 flex-1 text-[13px] leading-relaxed text-gray-800">
            {agent.description || agent.system_prompt || "No description."}
          </p>
          <div className="mt-4 flex items-center gap-2">
            <span className="font-mono text-[10.5px] text-gray-700">
              {agent.backend}
            </span>
            <Button
              size="sm"
              variant="outline"
              className="btn-restore ml-auto"
              onClick={() => onRestore(agent)}
            >
              Restore
            </Button>
          </div>
        </div>
      ))}
    </div>
  );
}
