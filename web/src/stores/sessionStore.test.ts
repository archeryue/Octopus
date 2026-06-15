import { describe, it, expect, beforeEach } from "vitest";
import { useSessionStore } from "./sessionStore";

describe("sessionStore", () => {
  beforeEach(() => {
    // Reset store between tests
    useSessionStore.setState({
      token: "",
      sessions: [],
      activeSessionId: null,
      messages: {},
      connected: false,
    });
  });

  it("sets and gets token", () => {
    const { setToken } = useSessionStore.getState();
    setToken("my-token");
    expect(useSessionStore.getState().token).toBe("my-token");
  });

  it("manages sessions list", () => {
    const { setSessions } = useSessionStore.getState();
    const sessions = [
      {
        id: "s1",
        name: "Session 1",
        working_dir: "/tmp",
        status: "idle" as const,
        created_at: "2026-01-01",
        message_count: 0,
        archived: false,
        origin: "user",
        backend: "claude-code" as const,
        can_fork: true,
        fork_is_full_copy: false,
      },
    ];
    setSessions(sessions);
    expect(useSessionStore.getState().sessions).toEqual(sessions);
  });

  it("sets active session id", () => {
    const { setActiveSessionId } = useSessionStore.getState();
    setActiveSessionId("s1");
    expect(useSessionStore.getState().activeSessionId).toBe("s1");
  });

  it("adds messages to session", () => {
    const { addMessage } = useSessionStore.getState();
    addMessage("s1", { role: "user", type: "text", content: "hello" });
    addMessage("s1", {
      role: "assistant",
      type: "text",
      content: "hi there",
    });

    const msgs = useSessionStore.getState().messages["s1"];
    expect(msgs).toHaveLength(2);
    expect(msgs[0].content).toBe("hello");
    expect(msgs[1].content).toBe("hi there");
  });

  it("sets messages for session", () => {
    const { setMessages } = useSessionStore.getState();
    const msgs = [{ role: "user" as const, type: "text", content: "test" }];
    setMessages("s1", msgs);
    expect(useSessionStore.getState().messages["s1"]).toEqual(msgs);
  });

  it("updates session status", () => {
    const { setSessions, updateSessionStatus } = useSessionStore.getState();
    setSessions([
      {
        id: "s1",
        name: "Test",
        working_dir: "/tmp",
        status: "idle",
        created_at: "2026-01-01",
        message_count: 0,
        archived: false,
        origin: "user",
        backend: "claude-code" as const,
        can_fork: true,
        fork_is_full_copy: false,
      },
    ]);

    updateSessionStatus("s1", "running");
    expect(useSessionStore.getState().sessions[0].status).toBe("running");
  });

  it("tracks connection status", () => {
    const { setConnected } = useSessionStore.getState();
    expect(useSessionStore.getState().connected).toBe(false);
    setConnected(true);
    expect(useSessionStore.getState().connected).toBe(true);
  });

  it("upsertBgTask appends new tasks and patches existing by id", () => {
    const { upsertBgTask } = useSessionStore.getState();
    const base = {
      session_id: "s1",
      command: "true",
      description: null,
      working_dir: "/tmp",
      exit_code: null,
      stdout: "",
      stderr: "",
      truncated: false,
      started_at: "t0",
      completed_at: null,
    };
    upsertBgTask("s1", { id: "a", status: "running", ...base });
    upsertBgTask("s1", { id: "b", status: "running", ...base });
    expect(useSessionStore.getState().bgTasks["s1"]).toHaveLength(2);
    // Patch the existing 'a' row in place; list length unchanged.
    upsertBgTask("s1", { id: "a", status: "completed", ...base, exit_code: 0 });
    const tasks = useSessionStore.getState().bgTasks["s1"];
    expect(tasks).toHaveLength(2);
    expect(tasks.find((t) => t.id === "a")?.status).toBe("completed");
    expect(tasks.find((t) => t.id === "a")?.exit_code).toBe(0);
  });

  it("setBgTasks replaces the entire list for a session", () => {
    const { upsertBgTask, setBgTasks } = useSessionStore.getState();
    const base = {
      session_id: "s1",
      command: "true",
      description: null,
      working_dir: "/tmp",
      exit_code: null,
      stdout: "",
      stderr: "",
      truncated: false,
      started_at: "t0",
      completed_at: null,
    };
    upsertBgTask("s1", { id: "old", status: "running", ...base });
    setBgTasks("s1", [{ id: "new", status: "completed", ...base, exit_code: 0 }]);
    const tasks = useSessionStore.getState().bgTasks["s1"];
    expect(tasks).toHaveLength(1);
    expect(tasks[0].id).toBe("new");
  });

  it("opens and closes the file viewer", () => {
    const { openViewer, closeViewer } = useSessionStore.getState();
    expect(useSessionStore.getState().viewer).toBeNull();
    openViewer("s1", "docs/plan.md");
    expect(useSessionStore.getState().viewer).toEqual({
      sessionId: "s1",
      path: "docs/plan.md",
    });
    openViewer("s2", "README.md");
    expect(useSessionStore.getState().viewer).toEqual({
      sessionId: "s2",
      path: "README.md",
    });
    closeViewer();
    expect(useSessionStore.getState().viewer).toBeNull();
  });

  it("preserves attachments on user messages", () => {
    const { addMessage } = useSessionStore.getState();
    const att = {
      id: "att123",
      filename: "screenshot.png",
      size: 1024,
      mime_type: "image/png",
    };
    addMessage("s1", {
      role: "user",
      type: "text",
      content: "what is this",
      attachments: [att],
    });
    const msgs = useSessionStore.getState().messages["s1"];
    expect(msgs).toHaveLength(1);
    expect(msgs[0].attachments).toEqual([att]);
  });

  it("manages agents, active agent, and upsert/remove", () => {
    const { setAgents, upsertAgent, removeAgent, setActiveAgentId } =
      useSessionStore.getState();
    const mk = (id: string, name: string, extra = {}) => ({
      id,
      name,
      description: "",
      avatar: null,
      system_prompt: "",
      model: null,
      credential_id: null,
      backend: "claude-code" as const,
      mcp_servers: ["ask", "bg"],
      tool_allow: "",
      tool_deny: "",
      is_system: false,
      archived: false,
      created_at: "2026-01-01",
      updated_at: "2026-01-01",
      active_session_count: 0,
      ...extra,
    });

    setAgents([mk("a1", "Default", { is_system: true })]);
    expect(useSessionStore.getState().agents).toHaveLength(1);

    // upsert appends a new agent, patches an existing one in place.
    upsertAgent(mk("a2", "Researcher"));
    expect(useSessionStore.getState().agents).toHaveLength(2);
    upsertAgent(mk("a2", "Renamed"));
    expect(useSessionStore.getState().agents).toHaveLength(2);
    expect(
      useSessionStore.getState().agents.find((a) => a.id === "a2")?.name
    ).toBe("Renamed");

    setActiveAgentId("a2");
    expect(useSessionStore.getState().activeAgentId).toBe("a2");

    removeAgent("a2");
    expect(useSessionStore.getState().agents.map((a) => a.id)).toEqual(["a1"]);
  });

  it("keeps messages separate per session", () => {
    const { addMessage } = useSessionStore.getState();
    addMessage("s1", { role: "user", type: "text", content: "msg for s1" });
    addMessage("s2", { role: "user", type: "text", content: "msg for s2" });

    const state = useSessionStore.getState();
    expect(state.messages["s1"]).toHaveLength(1);
    expect(state.messages["s2"]).toHaveLength(1);
    expect(state.messages["s1"][0].content).toBe("msg for s1");
    expect(state.messages["s2"][0].content).toBe("msg for s2");
  });

  it("manages connector installations and per-agent enablement", () => {
    const {
      setConnectorInstallations,
      upsertConnectorInstallation,
      removeConnectorInstallation,
      setAgentConnectorIds,
    } = useSessionStore.getState();
    const mk = (id: string, kind: string, label: string) => ({
      id,
      kind,
      label,
      auth_type: "oauth" as const,
      external_account_id: label,
      scopes: [],
      enable_by_default: false,
      needs_reconnect: false,
      token_expires_at: null,
      last_refresh_error_code: null,
      created_at: "2026-01-01",
    });

    setConnectorInstallations([mk("i1", "github", "octocat")]);
    expect(useSessionStore.getState().connectorInstallations).toHaveLength(1);

    // upsert appends a new one, patches existing in place.
    upsertConnectorInstallation(mk("i2", "gmail", "me@x.com"));
    expect(useSessionStore.getState().connectorInstallations).toHaveLength(2);
    upsertConnectorInstallation({ ...mk("i2", "gmail", "renamed@x.com") });
    expect(
      useSessionStore.getState().connectorInstallations.find((i) => i.id === "i2")
        ?.label
    ).toBe("renamed@x.com");

    // Enable both for an agent, then removing an installation prunes it from
    // every agent's enabled set.
    setAgentConnectorIds("a1", ["i1", "i2"]);
    expect(useSessionStore.getState().agentConnectorIds["a1"]).toEqual(["i1", "i2"]);
    removeConnectorInstallation("i1");
    expect(
      useSessionStore.getState().connectorInstallations.map((i) => i.id)
    ).toEqual(["i2"]);
    expect(useSessionStore.getState().agentConnectorIds["a1"]).toEqual(["i2"]);
  });
});
