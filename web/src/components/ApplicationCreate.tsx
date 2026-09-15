import { useState } from "react";
import { IconMenu2, IconSparkles, IconX } from "@tabler/icons-react";
import { createApplication } from "../api/applications";
import { useSessionStore } from "../stores/sessionStore";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Label } from "./ui/label";

/** The main-pane "new application" form (applications.md §7). Name +
 * description + which agent builds it; submitting opens a build session under
 * that agent and switches the pane to the new application, which shows the
 * build in progress. */
export function ApplicationCreate({
  onToggleSidebar,
}: {
  onToggleSidebar: () => void;
}) {
  const token = useSessionStore((s) => s.token);
  const agents = useSessionStore((s) => s.agents);
  const activeAgentId = useSessionStore((s) => s.activeAgentId);
  const upsertApplication = useSessionStore((s) => s.upsertApplication);
  const openApplication = useSessionStore((s) => s.openApplication);
  const showChat = useSessionStore((s) => s.showChat);

  const [name, setName] = useState("");
  const [icon, setIcon] = useState("");
  const [description, setDescription] = useState("");
  const [instructions, setInstructions] = useState("");
  const [pickedAgentId, setPickedAgentId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The builder defaults to whichever agent the sidebar has selected (else the
  // system one) and is only overridden by an explicit pick — derived rather
  // than synced through an effect, so an agent list arriving late can't stomp
  // a choice the user already made.
  const defaultAgentId = (
    agents.find((a) => a.id === activeAgentId) ??
    agents.find((a) => a.is_system) ??
    agents[0]
  )?.id;
  const agentId =
    pickedAgentId && agents.some((a) => a.id === pickedAgentId)
      ? pickedAgentId
      : defaultAgentId ?? "";

  const canSubmit =
    !busy && name.trim().length > 0 && description.trim().length > 0 && !!agentId;

  const submit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    try {
      const app = await createApplication(token, {
        name: name.trim(),
        description: description.trim(),
        agent_id: agentId,
        icon: icon.trim() || null,
        instructions: instructions.trim(),
      });
      upsertApplication(app);
      openApplication(app.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to create application");
      setBusy(false);
    }
  };

  return (
    <div className="application-create flex-1 flex flex-col min-h-0">
      <div className="application-header-bar flex items-center gap-3 px-4 h-12 shrink-0 border-b border-border bg-sidebar">
        <button
          className="btn btn-menu inline-flex items-center justify-center size-9 rounded-lg text-foreground hover:bg-accent md:hidden"
          onClick={onToggleSidebar}
          aria-label="Toggle sidebar"
        >
          <IconMenu2 size={18} />
        </button>
        <h3 className="text-[15px] font-semibold text-foreground truncate">
          New application
        </h3>
        <button
          className="btn-application-create-close ml-auto inline-flex h-8 w-8 items-center justify-center rounded-lg text-muted-foreground hover:bg-accent hover:text-foreground transition-colors"
          onClick={showChat}
          title="Close"
          aria-label="Close"
        >
          <IconX size={16} />
        </button>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto">
        <div className="mx-auto w-full max-w-2xl px-6 py-8 space-y-6">
          <div className="space-y-1.5">
            <h2 className="text-2xl font-bold tracking-tight text-foreground">
              Have an agent build you a web app
            </h2>
            <p className="text-sm text-muted-foreground leading-relaxed">
              Describe what you want. The agent writes it as a self-contained
              static site, and it shows up right here — no setup, no server.
            </p>
          </div>

          <div className="space-y-4">
            <div className="flex gap-3">
              <div className="space-y-2 w-20 shrink-0">
                <Label htmlFor="app-icon">Icon</Label>
                <Input
                  id="app-icon"
                  className="app-icon-input text-center"
                  value={icon}
                  onChange={(e) => setIcon(e.target.value)}
                  placeholder="🪟"
                  maxLength={4}
                />
              </div>
              <div className="space-y-2 flex-1">
                <Label htmlFor="app-name">Name</Label>
                <Input
                  id="app-name"
                  className="app-name-input"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="Habit Tracker"
                  autoFocus
                />
              </div>
            </div>

            <div className="space-y-2">
              <Label htmlFor="app-description">What should it do?</Label>
              <textarea
                id="app-description"
                className="app-description-input flex min-h-28 w-full rounded-lg border-[0.7px] border-gray-400 bg-input px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground outline-none transition-colors focus:border-primary focus:ring-[3px] focus:ring-primary/10"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="A daily habit tracker: add habits, tick them off each day, and show a 30-day streak grid."
              />
              <p className="text-xs text-muted-foreground">
                This is the brief the agent builds from — the more specific, the
                better the first version.
              </p>
            </div>

            <div className="space-y-2">
              <Label htmlFor="app-agent">Built by</Label>
              <select
                id="app-agent"
                className="app-agent-select flex h-9 w-full rounded-lg border-[0.7px] border-gray-400 bg-input px-3 text-sm text-foreground outline-none transition-colors focus:border-primary focus:ring-[3px] focus:ring-primary/10"
                value={agentId}
                onChange={(e) => setPickedAgentId(e.target.value)}
              >
                {agents.map((a) => (
                  <option key={a.id} value={a.id}>
                    {(a.avatar || "🐙") + "  " + a.name}
                  </option>
                ))}
              </select>
            </div>

            <details className="app-advanced group">
              <summary className="cursor-pointer text-sm text-muted-foreground hover:text-foreground transition-colors select-none">
                Extra instructions (optional)
              </summary>
              <textarea
                className="app-instructions-input mt-2 flex min-h-20 w-full rounded-lg border-[0.7px] border-gray-400 bg-input px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground outline-none transition-colors focus:border-primary focus:ring-[3px] focus:ring-primary/10"
                value={instructions}
                onChange={(e) => setInstructions(e.target.value)}
                placeholder="Dark theme. Keyboard shortcuts. No external fonts."
              />
            </details>

            {error && (
              <div className="app-create-error rounded-lg border-[0.7px] border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                {error}
              </div>
            )}

            <div className="flex items-center gap-3 pt-1">
              <Button
                className="btn-application-create"
                onClick={submit}
                disabled={!canSubmit}
              >
                <IconSparkles size={16} />
                {busy ? "Starting the build…" : "Build it"}
              </Button>
              <span className="text-xs text-muted-foreground">
                Opens a build session with the agent — you can watch it work.
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
