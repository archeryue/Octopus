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
});
