import { useState } from "react";
import { IconMenu2 } from "@tabler/icons-react";
import { AccountDropdown } from "./components/AccountDropdown";
import { AgentList } from "./components/AgentList";
import { AgentSettings } from "./components/AgentSettings";
import { ArchivedSessionsDialog } from "./components/ArchivedSessionsDialog";
import { ChatView } from "./components/ChatView";
import { ApplicationList } from "./components/ApplicationList";
import { ConnectorList } from "./components/ConnectorList";
import { CredentialList } from "./components/CredentialList";
// SessionList is rendered inside AgentList (nested under the active agent),
// not as its own sidebar section — sessions belong to an agent.
import { FileViewerDialog } from "./components/FileViewerDialog";
import { OctopusLogo } from "./components/OctopusLogo";
import { ScheduleList } from "./components/ScheduleList";
import { SchedulesDialog } from "./components/SchedulesDialog";
import { SettingsDialog } from "./components/SettingsDialog";
import { Button } from "./components/ui/button";
import { Input } from "./components/ui/input";
import { Label } from "./components/ui/label";
import { useViewportHeight } from "./hooks/useViewportHeight";
import { useWebSocket } from "./hooks/useWebSocket";
import { useSessionStore } from "./stores/sessionStore";

function App() {
  useViewportHeight();
  const token = useSessionStore((s) => s.token);
  const setToken = useSessionStore((s) => s.setToken);
  const [tokenInput, setTokenInput] = useState("");
  const [sidebarOpen, setSidebarOpen] = useState(false);

  if (!token) {
    const submit = () => {
      if (tokenInput.trim()) setToken(tokenInput.trim());
    };
    return (
      <div className="login-screen min-h-screen flex items-center justify-center bg-background p-6">
        <div className="w-full max-w-sm rounded-2xl border-[0.7px] border-border bg-card p-8 shadow-[0_8px_40px_-12px_rgba(20,23,29,0.12)]">
          <div className="space-y-1.5 mb-6">
            <h1 className="text-2xl font-bold tracking-tight text-foreground">
              Octopus
            </h1>
            <p className="text-sm text-muted-foreground leading-relaxed">
              Enter your access token to continue.
            </p>
          </div>
          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="token">Token</Label>
              <Input
                id="token"
                type="password"
                value={tokenInput}
                onChange={(e) => setTokenInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") submit();
                }}
                placeholder="Paste your token"
                autoFocus
              />
            </div>
            <Button className="btn-login w-full" onClick={submit}>
              Connect
            </Button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <AuthenticatedApp sidebarOpen={sidebarOpen} setSidebarOpen={setSidebarOpen} />
  );
}

function AuthenticatedApp({
  sidebarOpen,
  setSidebarOpen,
}: {
  sidebarOpen: boolean;
  setSidebarOpen: (v: boolean) => void;
}) {
  const { sendMessage, interrupt, approveTool, denyTool, answerQuestion } =
    useWebSocket();
  const connected = useSessionStore((s) => s.connected);
  const setToken = useSessionStore((s) => s.setToken);
  const agents = useSessionStore((s) => s.agents);
  const activeAgentId = useSessionStore((s) => s.activeAgentId);
  const [settingsOpen, setSettingsOpen] = useState(false);
  // Agent-settings dialog lives at the app level (not the sidebar) so it can
  // be driven from the account menu. It's a two-pane manager: this id just
  // seeds which agent is selected when it opens (`null` = the new-agent draft);
  // the dialog's own rail handles switching between agents after that.
  const [agentDialogOpen, setAgentDialogOpen] = useState(false);
  const [agentInitialId, setAgentInitialId] = useState<string | null>(null);
  // Management dialogs reached from the sidebar / account menu. Schedules is
  // also opened from chat (bare `/schedule`), so ChatView gets the opener too.
  const [schedulesOpen, setSchedulesOpen] = useState(false);
  const [archivedOpen, setArchivedOpen] = useState(false);

  const signOut = () => {
    setToken("");
    window.location.reload();
  };

  const openCreateAgent = () => {
    setAgentInitialId(null);
    setAgentDialogOpen(true);
  };
  // "Agent settings" in the account menu opens the manager focused on the
  // active agent (fall back to the system agent, then the new-agent draft if
  // there are somehow no agents).
  const openActiveAgentSettings = () => {
    const active =
      agents.find((a) => a.id === activeAgentId) ??
      agents.find((a) => a.is_system) ??
      null;
    setAgentInitialId(active?.id ?? null);
    setAgentDialogOpen(true);
  };

  return (
    <div className="app-layout">
      <aside
        className={`sidebar ${sidebarOpen ? "open" : ""}`}
        aria-label="Sidebar"
      >
        {/* Wordmark — vm0 has an org switcher here; we just brand it. */}
        <div className="shrink-0 flex items-center justify-between gap-2 px-3 pt-3 pb-2">
          <div className="flex items-center gap-2 min-w-0">
            <OctopusLogo size={22} className="text-primary shrink-0" />
            <span className="truncate text-base font-bold text-sidebar-foreground">
              Octopus
            </span>
          </div>
          <div className="flex items-center gap-0.5 shrink-0">
            <button
              type="button"
              className="md:hidden inline-flex h-8 w-8 items-center justify-center rounded-lg text-sidebar-foreground/70 hover:bg-sidebar-accent hover:text-sidebar-foreground transition-colors"
              onClick={() => setSidebarOpen(false)}
              aria-label="Close sidebar"
            >
              <IconMenu2 size={18} />
            </button>
          </div>
        </div>

        {/* Scrollable middle: sessions / schedules / harness sections.
         * px-5 on the nav inset + px-3 on each item pill = 32px from
         * sidebar edge to item text. Hover pill itself insets 20px. */}
        <nav className="flex-1 flex flex-col min-h-0 overflow-y-auto px-3">
          <AgentList onCreateAgent={openCreateAgent} />
          <ScheduleList onOpen={() => setSchedulesOpen(true)} />
          <ApplicationList onAdd={() => {}} />
          <ConnectorList />
          <CredentialList />
        </nav>

        {/* Account footer — the single home for settings (no sidebar gears). */}
        <div className="shrink-0 border-t border-sidebar-border px-3 py-2">
          <AccountDropdown
            onSignOut={signOut}
            onOpenSettings={() => setSettingsOpen(true)}
            onOpenAgentSettings={openActiveAgentSettings}
            onOpenArchivedSessions={() => setArchivedOpen(true)}
          />
        </div>
      </aside>

      <div className="main-area">
        <ChatView
          sendMessage={sendMessage}
          interrupt={interrupt}
          approveTool={approveTool}
          denyTool={denyTool}
          answerQuestion={answerQuestion}
          connected={connected}
          onToggleSidebar={() => setSidebarOpen(!sidebarOpen)}
          onOpenSchedules={() => setSchedulesOpen(true)}
        />
      </div>

      {sidebarOpen && (
        <div
          className="sidebar-overlay"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      <SettingsDialog open={settingsOpen} onOpenChange={setSettingsOpen} />
      <AgentSettings
        open={agentDialogOpen}
        onOpenChange={setAgentDialogOpen}
        initialAgentId={agentInitialId}
      />
      <SchedulesDialog open={schedulesOpen} onOpenChange={setSchedulesOpen} />
      <ArchivedSessionsDialog
        open={archivedOpen}
        onOpenChange={setArchivedOpen}
      />
      <FileViewerDialog />
    </div>
  );
}

export default App;
