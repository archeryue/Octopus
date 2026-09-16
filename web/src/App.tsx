import { useState } from "react";
import { IconMenu2 } from "@tabler/icons-react";
import { AgentFormPage } from "./components/AgentFormPage";
import { ApplicationFormPage } from "./components/ApplicationFormPage";
import { ApplicationView } from "./components/ApplicationView";
import { ArchivedSessionsDialog } from "./components/ArchivedSessionsDialog";
import { ChatView } from "./components/ChatView";
import { ConnectorsPage } from "./components/ConnectorsPage";
import { FileViewerDialog } from "./components/FileViewerDialog";
import { HarnessPage } from "./components/HarnessPage";
import { OctopusLogo } from "./components/OctopusLogo";
import { SchedulesPage } from "./components/SchedulesPage";
import { SettingsDialog } from "./components/SettingsDialog";
import { SidebarAccount } from "./components/SidebarAccount";
import { SidebarAgents } from "./components/SidebarAgents";
import { SidebarApplications } from "./components/SidebarApplications";
import { SidebarEdgeToggle } from "./components/SidebarEdgeToggle";
import { SidebarManage } from "./components/SidebarManage";
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
      <div className="login-screen flex min-h-screen items-center justify-center bg-gray-50 p-6">
        <div className="w-full max-w-sm rounded-2xl border border-gray-300 bg-card p-8 shadow-[0_24px_60px_-28px_rgba(28,44,72,0.28)]">
          <h1 className="mb-6 text-2xl font-bold tracking-tight text-gray-950">
            Octopus
          </h1>
          <p className="mb-6 text-sm leading-relaxed text-gray-800">
            Enter your access token to continue.
          </p>
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
  const mainView = useSessionStore((s) => s.mainView);
  const sidebarCollapsed = useSessionStore((s) => s.sidebarCollapsed);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [archivedOpen, setArchivedOpen] = useState(false);

  const signOut = () => {
    setToken("");
    window.location.reload();
  };
  const toggleSidebar = () => setSidebarOpen(!sidebarOpen);

  return (
    <div className={`app-layout ${sidebarCollapsed ? "sidebar-collapsed" : ""}`}>
      <aside
        className={`sidebar ${sidebarOpen ? "open" : ""} ${
          sidebarCollapsed ? "collapsed" : ""
        }`}
        aria-label="Sidebar"
      >
        {/* Brand lockup. The mark is untouched — same artwork, same 22px, same
         * brand navy it has always been; only the wordmark beside it follows
         * the console design. */}
        <div className="sidebar-brand flex h-12 shrink-0 items-center gap-2.5 px-[18px]">
          <OctopusLogo size={22} className="shrink-0" />
          <span className="brand-name truncate text-[17px] font-bold text-gray-950">
            Octopus
          </span>
          <button
            type="button"
            className="ml-auto inline-flex size-8 items-center justify-center rounded-lg text-gray-700 transition-colors hover:bg-gray-100 md:hidden"
            onClick={() => setSidebarOpen(false)}
            aria-label="Close sidebar"
          >
            <IconMenu2 size={18} />
          </button>
        </div>

        {/* Workspace (what you made) above, system (what runs it) below. */}
        <nav className="sidebar-nav flex min-h-0 flex-1 flex-col overflow-y-auto px-3 pb-3">
          <SidebarAgents />
          <SidebarApplications />
          <SidebarManage />
        </nav>

        <div className="sidebar-account-bar shrink-0 border-t border-gray-300 px-3 py-2.5">
          <SidebarAccount
            onSignOut={signOut}
            onOpenSettings={() => setSettingsOpen(true)}
            onOpenArchivedSessions={() => setArchivedOpen(true)}
          />
        </div>
      </aside>

      <SidebarEdgeToggle />

      {/* ChatView stays mounted behind every other view — it owns the composer
       * draft, the scroll position and pending attachments, and unmounting it
       * on a trip to Schedules would throw all of that away. */}
      <div className="main-area">
        <div
          className={`main-pane flex min-h-0 flex-1 flex-col ${
            mainView === "chat" ? "" : "hidden"
          }`}
        >
          <ChatView
            sendMessage={sendMessage}
            interrupt={interrupt}
            approveTool={approveTool}
            denyTool={denyTool}
            answerQuestion={answerQuestion}
            connected={connected}
            onToggleSidebar={toggleSidebar}
            onOpenSchedules={() =>
              useSessionStore.getState().openManage("schedules")
            }
          />
        </div>
        {mainView === "application" && (
          <ApplicationView onToggleSidebar={toggleSidebar} />
        )}
        {mainView === "application-create" && (
          <ApplicationFormPage onToggleSidebar={toggleSidebar} />
        )}
        {mainView === "agent-form" && (
          <AgentFormPage onToggleSidebar={toggleSidebar} />
        )}
        {mainView === "schedules" && (
          <SchedulesPage onToggleSidebar={toggleSidebar} />
        )}
        {mainView === "connectors" && (
          <ConnectorsPage onToggleSidebar={toggleSidebar} />
        )}
        {mainView === "harness" && (
          <HarnessPage onToggleSidebar={toggleSidebar} />
        )}
      </div>

      {sidebarOpen && (
        <div className="sidebar-overlay" onClick={() => setSidebarOpen(false)} />
      )}

      <SettingsDialog open={settingsOpen} onOpenChange={setSettingsOpen} />
      <ArchivedSessionsDialog open={archivedOpen} onOpenChange={setArchivedOpen} />
      <FileViewerDialog />
    </div>
  );
}

export default App;
