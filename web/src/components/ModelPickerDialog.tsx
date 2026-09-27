import { useMemo, useState } from "react";
import { IconCheck, IconCpu } from "@tabler/icons-react";
import { useSessionStore, type SessionInfo } from "../stores/sessionStore";
import { buildModelOptions } from "../lib/modelOptions";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "./ui/dialog";
import { Input } from "./ui/input";
import { Button } from "./ui/button";

const API_URL = window.location.origin;

/**
 * `/model` — which model runs *this* conversation.
 *
 * Per session, not per agent: wanting a stronger model for one hard question is
 * not wanting to re-point every session the agent owns. Clearing the override
 * puts the session back to following its agent, so the agent stays the place
 * where the durable answer lives.
 *
 * The free-text field is not a fallback, it is the escape hatch that makes the
 * list safe to keep short: both CLIs accept names this build cannot know, and a
 * model released next week must not need an Octopus release to be usable.
 */
export function ModelPickerDialog({
  session,
  open,
  onOpenChange,
  onApplied,
}: {
  session: SessionInfo;
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onApplied?: (model: string | null) => void;
}) {
  const token = useSessionStore((s) => s.token);
  const agents = useSessionStore((s) => s.agents);
  const sessions = useSessionStore((s) => s.sessions);
  const backendModels = useSessionStore((s) => s.backendModels);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const agent = agents.find((a) => a.id === session.agent_id) ?? null;
  const options = useMemo(
    () =>
      buildModelOptions({
        sessionModel: session.model,
        agentModel: agent?.model,
        backendModels: backendModels[session.backend],
        inUse: [
          ...agents.map((a) => a.model),
          ...sessions.map((s) => s.model),
        ],
      }),
    [session.model, session.backend, agent?.model, backendModels, agents, sessions]
  );

  const apply = async (model: string | null) => {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${API_URL}/api/sessions/${session.id}`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ model }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const updated: SessionInfo = await res.json();
      // Keep the list in step so the composer hint and this dialog agree
      // without a refetch.
      useSessionStore
        .getState()
        .setSessions(
          sessions.map((s) => (s.id === updated.id ? { ...s, ...updated } : s))
        );
      onApplied?.(updated.model ?? null);
      onOpenChange(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "could not change the model");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="model-picker-dialog max-w-md">
        <DialogHeader>
          <DialogTitle>Model for this session</DialogTitle>
          <DialogDescription>
            Applies from the next turn, to this conversation only — the agent's
            own model is unchanged.
          </DialogDescription>
        </DialogHeader>

        <div className="model-options flex flex-col gap-1">
          {options.map((o) => (
            <button
              key={o.value ?? "__agent__"}
              type="button"
              disabled={busy}
              className="btn-model-option group flex items-center gap-2 rounded-lg border border-border bg-card px-3 py-2 text-left text-sm hover:bg-accent disabled:opacity-50"
              onClick={() => apply(o.value)}
            >
              <IconCpu size={14} className="shrink-0 text-muted-foreground" />
              <span className="min-w-0 flex-1 truncate">{o.label}</span>
              {o.source === "used" && o.value !== null && (
                <span className="shrink-0 font-mono text-[10px] text-muted-foreground">
                  in use
                </span>
              )}
              {o.current && (
                <IconCheck size={14} className="shrink-0 text-primary" />
              )}
            </button>
          ))}
        </div>

        <form
          className="model-freeform flex items-center gap-2 pt-1"
          onSubmit={(e) => {
            e.preventDefault();
            const name = typed.trim();
            if (name) void apply(name);
          }}
        >
          <Input
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            placeholder="…or type any model name"
            className="h-9 text-sm"
          />
          <Button type="submit" size="sm" disabled={busy || !typed.trim()}>
            Use
          </Button>
        </form>

        {error && (
          <p className="model-error text-xs text-destructive">{error}</p>
        )}
      </DialogContent>
    </Dialog>
  );
}
