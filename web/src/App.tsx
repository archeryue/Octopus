import { useState } from "react";
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
        <div className="w-full max-w-sm rounded-lg border border-border bg-card p-8 shadow-2xl">
          <div className="space-y-1.5 mb-6">
            <h1 className="text-2xl font-semibold tracking-tight text-foreground">
              Octopus
            </h1>
            <p className="text-sm text-muted-foreground">
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

  return <AuthenticatedApp sidebarOpen={sidebarOpen} setSidebarOpen={setSidebarOpen} />;
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

  return (
    <div className="app-layout">
      <div className={`sidebar ${sidebarOpen ? "open" : ""}`}>
        <SessionList />
        <ScheduleList />
        <CredentialList />
        <button
          className="btn-logout mt-auto w-full border-t border-border px-4 py-3 text-sm text-muted-foreground hover:bg-accent hover:text-foreground transition-colors"
          onClick={() => {
            setToken("");
            window.location.reload();
          }}
        >
          Logout
        </button>
      </div>

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
        <div className="sidebar-overlay" onClick={() => setSidebarOpen(false)} />
      )}
    </div>
  );
}

export default App;
