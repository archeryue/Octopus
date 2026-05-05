import { create } from "zustand";

export type SessionStatus = "idle" | "running" | "waiting_approval";

export interface SessionInfo {
  id: string;
  name: string;
  working_dir: string;
  status: SessionStatus;
  created_at: string;
  message_count: number;
}

export interface Schedule {
  id: string;
  session_id: string;
  name: string;
  prompt: string;
  interval_seconds: number;
  enabled: boolean;
  created_at: string;
  last_run_at: string | null;
}

export interface Message {
  role: "user" | "assistant" | "system" | "tool";
  type: string;
  content?: string;
  tool_name?: string;
  tool_input?: Record<string, unknown>;
  tool_use_id?: string;
  is_error?: boolean;
  session_id?: string;
  cost?: number;
}

interface SessionStore {
  token: string;
  setToken: (t: string) => void;

  sessions: SessionInfo[];
  setSessions: (s: SessionInfo[]) => void;
  updateSessionStatus: (id: string, status: SessionStatus) => void;

  activeSessionId: string | null;
  setActiveSessionId: (id: string | null) => void;

  messages: Record<string, Message[]>;
  addMessage: (sessionId: string, msg: Message) => void;
  setMessages: (sessionId: string, msgs: Message[]) => void;

  schedules: Schedule[];
  setSchedules: (s: Schedule[]) => void;

  // Per-session queue of messages waiting for the current run to finish.
  // Mirrored from server `queued` / `dequeued` events; not persisted.
  pendingQueue: Record<string, string[]>;
  setPendingQueue: (sessionId: string, queue: string[]) => void;
  enqueuePending: (sessionId: string, content: string) => void;
  dequeuePending: (sessionId: string) => void;
  clearPending: (sessionId: string) => void;

  connected: boolean;
  setConnected: (c: boolean) => void;
}

export const useSessionStore = create<SessionStore>((set) => ({
  token: localStorage.getItem("octopus_token") || "",
  setToken: (t) => {
    localStorage.setItem("octopus_token", t);
    set({ token: t });
  },

  sessions: [],
  setSessions: (sessions) => set({ sessions }),
  updateSessionStatus: (id, status) =>
    set((s) => ({
      sessions: s.sessions.map((sess) =>
        sess.id === id ? { ...sess, status } : sess
      ),
    })),

  activeSessionId: null,
  setActiveSessionId: (id) => set({ activeSessionId: id }),

  messages: {},
  addMessage: (sessionId, msg) =>
    set((s) => ({
      messages: {
        ...s.messages,
        [sessionId]: [...(s.messages[sessionId] || []), msg],
      },
    })),
  setMessages: (sessionId, msgs) =>
    set((s) => ({
      messages: { ...s.messages, [sessionId]: msgs },
    })),

  schedules: [],
  setSchedules: (schedules) => set({ schedules }),

  pendingQueue: {},
  setPendingQueue: (sessionId, queue) =>
    set((s) => {
      const next = { ...s.pendingQueue };
      if (queue.length === 0) delete next[sessionId];
      else next[sessionId] = queue;
      return { pendingQueue: next };
    }),
  enqueuePending: (sessionId, content) =>
    set((s) => ({
      pendingQueue: {
        ...s.pendingQueue,
        [sessionId]: [...(s.pendingQueue[sessionId] || []), content],
      },
    })),
  dequeuePending: (sessionId) =>
    set((s) => {
      const cur = s.pendingQueue[sessionId] || [];
      if (cur.length === 0) return s;
      return {
        pendingQueue: { ...s.pendingQueue, [sessionId]: cur.slice(1) },
      };
    }),
  clearPending: (sessionId) =>
    set((s) => {
      if (!s.pendingQueue[sessionId]) return s;
      const next = { ...s.pendingQueue };
      delete next[sessionId];
      return { pendingQueue: next };
    }),

  connected: false,
  setConnected: (c) => set({ connected: c }),
}));
