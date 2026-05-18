import { useState } from "react";
import { IconMenu2 } from "@tabler/icons-react";
import { AccountDropdown } from "./components/AccountDropdown";
import { ChatView } from "./components/ChatView";
import { CredentialList } from "./components/CredentialList";
import { ScheduleList } from "./components/ScheduleList";
import { SessionList } from "./components/SessionList";
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
        <div className="w-full max-w-sm rounded-2xl border-[0.7px] border-border bg-card p-10 shadow-[0_8px_40px_-12px_rgba(20,23,29,0.12)]">
          <div className="space-y-2 mb-8">
            <h1 className="text-2xl font-bold tracking-tight text-foreground">
              Octopus
            </h1>
            <p className="text-sm text-muted-foreground leading-relaxed">
              Enter your access token to continue.
            </p>
          </div>
          <div className="space-y-5">
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

  const signOut = () => {
    setToken("");
    window.location.reload();
  };

  return (
    <div className="app-layout">
      <aside
        className={`sidebar ${sidebarOpen ? "open" : ""}`}
        aria-label="Sidebar"
      >
        {/* Wordmark — vm0 has an org switcher here; we just brand it. */}
        <div className="shrink-0 flex items-center justify-between gap-2 px-8 pt-6 pb-4">
          <div className="flex items-center gap-2.5 min-w-0">
            <span className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-primary text-primary-foreground text-sm font-bold">
              O
            </span>
            <span className="truncate text-base font-bold text-sidebar-foreground">
              Octopus
            </span>
          </div>
          <button
            type="button"
            className="md:hidden inline-flex h-8 w-8 items-center justify-center rounded-lg text-sidebar-foreground/70 hover:bg-sidebar-accent hover:text-sidebar-foreground transition-colors"
            onClick={() => setSidebarOpen(false)}
            aria-label="Close sidebar"
          >
            <IconMenu2 size={18} />
          </button>
        </div>

        {/* Scrollable middle: sessions / schedules / harness sections.
         * px-5 on the nav inset + px-3 on each item pill = 32px from
         * sidebar edge to item text. Hover pill itself insets 20px. */}
        <nav className="flex-1 flex flex-col min-h-0 overflow-y-auto px-8 pt-3">
          <SessionList />
          <ScheduleList />
          <CredentialList />
        </nav>

        {/* Account footer. */}
        <div className="shrink-0 border-t border-sidebar-border px-8 py-4">
          <AccountDropdown onSignOut={signOut} />
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
        />
      </div>

      {sidebarOpen && (
        <div
          className="sidebar-overlay"
          onClick={() => setSidebarOpen(false)}
        />
      )}
    </div>
  );
}

export default App;
