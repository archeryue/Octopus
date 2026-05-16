import { useState } from "react";
import { ChatView } from "./components/ChatView";
import { ScheduleList } from "./components/ScheduleList";
import { SessionList } from "./components/SessionList";
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
    return (
      <div className="login-screen">
        <div className="login-card">
          <h1>Octopus</h1>
          <p>Enter your access token</p>
          <input
            type="password"
            value={tokenInput}
            onChange={(e) => setTokenInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && tokenInput.trim()) {
                setToken(tokenInput.trim());
              }
            }}
            placeholder="Token"
            autoFocus
          />
          <button
            className="btn btn-login"
            onClick={() => tokenInput.trim() && setToken(tokenInput.trim())}
          >
            Connect
          </button>
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
  const { sendMessage, interrupt, approveTool, denyTool } = useWebSocket();
  const connected = useSessionStore((s) => s.connected);
  const setToken = useSessionStore((s) => s.setToken);

  return (
    <div className="app-layout">
      <div className={`sidebar ${sidebarOpen ? "open" : ""}`}>
        <SessionList />
        <ScheduleList />
        <button
          className="btn btn-logout"
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
