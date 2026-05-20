import { useEffect, useState } from "react";
import { useSessionStore, type Agent } from "../stores/sessionStore";
import { Button } from "./ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "./ui/dialog";
import { Input } from "./ui/input";
import { Label } from "./ui/label";

const API = `${window.location.origin}/api/agents`;
const BUILTIN_MCP = ["ask", "bg", "viewer"] as const;

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  /** The agent being edited, or null to create a new one. */
  agent: Agent | null;
}

const textareaCls =
  "flex w-full rounded-lg border-[0.7px] border-gray-400 bg-input px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground outline-none transition-colors focus:border-primary focus:ring-[3px] focus:ring-primary/10 resize-none";

export function AgentSettings({ open, onOpenChange, agent }: Props) {
  const token = useSessionStore((s) => s.token);
  const credentials = useSessionStore((s) => s.credentials);
  const upsertAgent = useSessionStore((s) => s.upsertAgent);
  const removeAgent = useSessionStore((s) => s.removeAgent);
  const setActiveAgentId = useSessionStore((s) => s.setActiveAgentId);
  const sessions = useSessionStore((s) => s.sessions);
  const setSessions = useSessionStore((s) => s.setSessions);
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const setActiveSessionId = useSessionStore((s) => s.setActiveSessionId);
  const claudeCreds = credentials.filter((c) => c.backend === "claude-code");

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [avatar, setAvatar] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [model, setModel] = useState("");
  const [credentialId, setCredentialId] = useState("");
  const [mcpServers, setMcpServers] = useState<string[]>([...BUILTIN_MCP]);
  const [toolAllow, setToolAllow] = useState("");
  const [toolDeny, setToolDeny] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // (Re)seed the form whenever the dialog opens or the target agent changes.
  useEffect(() => {
    if (!open) return;
    setError(null);
    setName(agent?.name ?? "");
    setDescription(agent?.description ?? "");
    setAvatar(agent?.avatar ?? "");
    setSystemPrompt(agent?.system_prompt ?? "");
    setModel(agent?.model ?? "");
    setCredentialId(agent?.credential_id ?? "");
    setMcpServers(agent?.mcp_servers ?? [...BUILTIN_MCP]);
    setToolAllow(agent?.tool_allow ?? "");
    setToolDeny(agent?.tool_deny ?? "");
  }, [open, agent]);

  const headers = {
    "Content-Type": "application/json",
    Authorization: `Bearer ${token}`,
  };

  const toggleMcp = (id: string) =>
    setMcpServers((cur) =>
      cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]
    );

  const detailOf = async (res: Response): Promise<string> => {
    const b = await res.json().catch(() => null);
    return (b && typeof b.detail === "string" && b.detail) || `HTTP ${res.status}`;
  };

  const save = async () => {
    if (!name.trim()) {
      setError("Name is required");
      return;
    }
    setSaving(true);
    setError(null);
    const body = {
      name: name.trim(),
      description,
      avatar: avatar.trim() || null,
      system_prompt: systemPrompt,
      model: model.trim() || null,
      credential_id: credentialId || null,
      mcp_servers: mcpServers,
      tool_allow: toolAllow,
      tool_deny: toolDeny,
    };
    try {
      const res = agent
        ? await fetch(`${API}/${agent.id}`, {
            method: "PATCH",
            headers,
            body: JSON.stringify(body),
          })
        : await fetch(API, {
            method: "POST",
            headers,
            body: JSON.stringify(body),
          });
      if (res.ok) {
        const saved: Agent = await res.json();
        upsertAgent(saved);
        if (!agent) setActiveAgentId(saved.id);
        onOpenChange(false);
      } else {
        setError(await detailOf(res));
      }
    } catch {
      setError("Network error");
    } finally {
      setSaving(false);
    }
  };

  const archive = async () => {
    if (!agent) return;
    const res = await fetch(`${API}/${agent.id}/archive`, {
      method: "POST",
      headers,
    });
    if (res.ok) {
      // The backend cascade-archives this agent's sessions; mirror that in the
      // store so they vanish from the sidebar, and clear the active session if
      // it was one of them. Re-selecting a fallback agent (Octo) is handled by
      // AgentList's auto-select effect once activeAgentId is cleared.
      const orphaned = new Set(
        sessions.filter((s) => s.agent_id === agent.id).map((s) => s.id)
      );
      if (orphaned.size) {
        setSessions(sessions.filter((s) => !orphaned.has(s.id)));
      }
      if (activeSessionId && orphaned.has(activeSessionId)) {
        setActiveSessionId(null);
      }
      removeAgent(agent.id);
      setActiveAgentId(null);
      onOpenChange(false);
    } else {
      setError(await detailOf(res));
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="agent-settings">
        <DialogHeader>
          <DialogTitle>{agent ? "Agent settings" : "New agent"}</DialogTitle>
          <DialogDescription>
            An agent is a durable assistant: its system prompt, model, tools
            and schedules persist across sessions.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="flex gap-2">
            <div className="w-16 space-y-1.5">
              <Label htmlFor="agent-avatar">Icon</Label>
              <Input
                id="agent-avatar"
                value={avatar}
                onChange={(e) => setAvatar(e.target.value)}
                placeholder="🐙"
                className="h-9 text-center"
              />
            </div>
            <div className="flex-1 space-y-1.5">
              <Label htmlFor="agent-name">Name</Label>
              <Input
                id="agent-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Researcher"
                className="h-9"
                autoFocus
              />
            </div>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="agent-desc">Description</Label>
            <Input
              id="agent-desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="What this agent is for (optional)"
              className="h-9"
            />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="agent-prompt">System prompt</Label>
            <textarea
              id="agent-prompt"
              value={systemPrompt}
              onChange={(e) => setSystemPrompt(e.target.value)}
              rows={4}
              placeholder="You are a meticulous research assistant…"
              className={textareaCls}
            />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="agent-model">Model</Label>
            <Input
              id="agent-model"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder="claude-opus-4-7 (blank = backend default)"
              className="h-9"
            />
          </div>

          {claudeCreds.length > 0 && (
            <div className="space-y-1.5">
              <Label htmlFor="agent-cred">Credential</Label>
              <select
                id="agent-cred"
                className="agent-credential-select flex h-9 w-full rounded-md border border-border bg-input px-3 py-1 text-sm text-foreground outline-none focus:border-ring focus:ring-2 focus:ring-ring/30"
                value={credentialId}
                onChange={(e) => setCredentialId(e.target.value)}
              >
                <option value="">Default auth (CLI login)</option>
                {claudeCreds.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.label}
                  </option>
                ))}
              </select>
            </div>
          )}

          <div className="space-y-1.5">
            <Label>Built-in tools</Label>
            <div className="flex gap-3">
              {BUILTIN_MCP.map((id) => (
                <label
                  key={id}
                  className="flex items-center gap-1.5 text-sm text-foreground"
                >
                  <input
                    type="checkbox"
                    checked={mcpServers.includes(id)}
                    onChange={() => toggleMcp(id)}
                  />
                  {id}
                </label>
              ))}
            </div>
          </div>

          <div className="grid grid-cols-2 gap-2">
            <div className="space-y-1.5">
              <Label htmlFor="agent-allow">Allow tools</Label>
              <textarea
                id="agent-allow"
                value={toolAllow}
                onChange={(e) => setToolAllow(e.target.value)}
                rows={3}
                placeholder="one per line; blank = all"
                className={textareaCls}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="agent-deny">Deny tools</Label>
              <textarea
                id="agent-deny"
                value={toolDeny}
                onChange={(e) => setToolDeny(e.target.value)}
                rows={3}
                placeholder="one per line; wins over allow"
                className={textareaCls}
              />
            </div>
          </div>

          {error && <p className="text-sm text-destructive">{error}</p>}
        </div>

        <DialogFooter className="flex items-center justify-between gap-2 sm:justify-between">
          {agent && !agent.is_system ? (
            <Button variant="destructive" size="sm" onClick={archive}>
              Archive agent
            </Button>
          ) : (
            <span />
          )}
          <Button
            className="btn-agent-save"
            size="sm"
            onClick={save}
            disabled={saving}
          >
            {saving ? "Saving…" : "Save"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
