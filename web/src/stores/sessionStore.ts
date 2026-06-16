import { create } from "zustand";
import type {
  AgentRead as ApiAgentRead,
  AttachmentMetadata as ApiAttachmentMetadata,
  BackendKind as ApiBackendKind,
  ConnectorCatalogEntry as ApiConnectorCatalogEntry,
  ConnectorInstallationInfo as ApiConnectorInstallationInfo,
  CredentialInfo as ApiCredentialInfo,
  ScheduleInfo,
  SessionInfo as ApiSessionInfo,
  SessionStatus as ApiSessionStatus,
} from "../api";

// Re-export contract types under the names the rest of the frontend
// already uses. Source of truth is `web/src/api/contracts.ts`, regenerated
// from FastAPI's openapi.json via `bun run generate:contracts`.
export type SessionStatus = ApiSessionStatus;
export type SessionInfo = ApiSessionInfo;
export type Agent = ApiAgentRead;
export type BackendKind = ApiBackendKind;
export type CredentialInfo = ApiCredentialInfo;
export type ConnectorCatalogEntry = ApiConnectorCatalogEntry;
export type ConnectorInstallationInfo = ApiConnectorInstallationInfo;
export type Schedule = ScheduleInfo;
export type AttachmentMetadata = ApiAttachmentMetadata;

// `Message` is a UI-only shape: it's how WS events are normalized for
// rendering, not 1-to-1 with `MessageContent` from the contract (which has
// `content: unknown` because tool_result can carry arbitrary JSON). Leave
// hand-rolled.
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
  // User-uploaded files attached to this message. Present on user
  // messages that the user attached files to (image, PDF, anything).
  // The chat UI renders thumbnails / file chips below the message text.
  attachments?: AttachmentMetadata[];
  // Per-session sequence number, present on messages loaded from the
  // session detail snapshot. Used as the rewind target for "Fork from here"
  // (session-rewind.md §6.1). Absent on freshly-streamed messages until
  // the next detail reload.
  seq?: number;
}

export interface QuestionOption {
  label: string;
  description?: string;
}

export interface QuestionItem {
  question: string;
  header?: string;
  multiSelect?: boolean;
  options: QuestionOption[];
}

export interface PendingQuestion {
  question_id: string;
  questions: QuestionItem[];
}

interface SessionStore {
  token: string;
  setToken: (t: string) => void;

  // Agents own sessions/schedules/bridges (agent-refactor.md). The sidebar
  // is two-pane: pick an agent, then see its sessions. `activeAgentId`
  // drives the session/schedule filters.
  agents: Agent[];
  setAgents: (a: Agent[]) => void;
  upsertAgent: (a: Agent) => void;
  removeAgent: (id: string) => void;
  activeAgentId: string | null;
  setActiveAgentId: (id: string | null) => void;

  // Which AI backends this host can run (GET /api/backends). 'claude-code'
  // is always present; 'codex' only when the binary resolves. Drives the
  // backend selector in the session-create form (codex-backend.md §6).
  availableBackends: string[];
  setAvailableBackends: (b: string[]) => void;

  sessions: SessionInfo[];
  setSessions: (s: SessionInfo[]) => void;
  updateSessionStatus: (id: string, status: SessionStatus) => void;

  // Mirror of `archived=true` rows from `GET /api/sessions?include_archived=true`.
  // SessionList fetches into this lazily when the user expands the
  // archived section; ChatView reads it so it can detect when the
  // active session is archived (show read-only banner, hide input).
  archivedSessions: SessionInfo[];
  setArchivedSessions: (s: SessionInfo[]) => void;

  activeSessionId: string | null;
  setActiveSessionId: (id: string | null) => void;

  messages: Record<string, Message[]>;
  addMessage: (sessionId: string, msg: Message) => void;
  setMessages: (sessionId: string, msgs: Message[]) => void;

  // Per-session WS-event dedup baseline. Set when a snapshot is loaded
  // (`/api/sessions/{id}` returns `next_message_seq`); the WS handler
  // drops any event whose `seq <= lastAppliedSeq[sessionId]` so a
  // reconnect-refetch can't stomp an event that arrived between the
  // snapshot's SQL and `setMessages` (the original race).
  lastAppliedSeq: Record<string, number>;
  setLastAppliedSeq: (sessionId: string, seq: number) => void;

  schedules: Schedule[];
  setSchedules: (s: Schedule[]) => void;

  credentials: CredentialInfo[];
  setCredentials: (c: CredentialInfo[]) => void;

  // Connectors (connectors.md). Installations are global; the catalog lists
  // installable kinds. Per-agent enablement (which installations an agent may
  // call) is keyed by agentId → installation ids.
  connectorCatalog: ConnectorCatalogEntry[];
  setConnectorCatalog: (c: ConnectorCatalogEntry[]) => void;
  connectorInstallations: ConnectorInstallationInfo[];
  setConnectorInstallations: (c: ConnectorInstallationInfo[]) => void;
  upsertConnectorInstallation: (c: ConnectorInstallationInfo) => void;
  removeConnectorInstallation: (id: string) => void;
  agentConnectorIds: Record<string, string[]>;
  setAgentConnectorIds: (agentId: string, ids: string[]) => void;

  // Per-session queue of messages waiting for the current run to finish.
  // Mirrored from server `queued` / `dequeued` events; not persisted.
  pendingQueue: Record<string, string[]>;
  setPendingQueue: (sessionId: string, queue: string[]) => void;
  enqueuePending: (sessionId: string, content: string) => void;
  dequeuePending: (sessionId: string) => void;
  clearPending: (sessionId: string) => void;

  // Deferred /fork requests (session-fork.md). When `/fork` is typed
  // while a session is busy, we record the intent here instead of erroring;
  // a watcher fires the duplicate once the session goes idle + drained.
  // Keyed by the PARENT session id → the requested label (null = default
  // name). Tab-scoped (not persisted) — closing the tab drops it.
  pendingForks: Record<string, { label: string | null }>;
  setPendingFork: (sessionId: string, label: string | null) => void;
  clearPendingFork: (sessionId: string) => void;

  // Active AskUserQuestion prompts waiting for the user's answer.
  pendingQuestions: Record<string, PendingQuestion[]>;
  setPendingQuestions: (sessionId: string, qs: PendingQuestion[]) => void;
  addPendingQuestion: (sessionId: string, q: PendingQuestion) => void;
  removePendingQuestion: (sessionId: string, questionId: string) => void;

  connected: boolean;
  setConnected: (c: boolean) => void;

  // FileViewerDialog is mounted at the App level and reads this slot.
  // null = closed; non-null = open and fetching the named file. Set
  // by ChatView when the `/showme` resolver returns a concrete path.
  viewer: { sessionId: string; path: string } | null;
  openViewer: (sessionId: string, path: string) => void;
  closeViewer: () => void;

  // Cross-turn background tasks. Keyed by sessionId → list of tasks
  // (most-recent last as they arrive over WS / from snapshot fetch).
  // The BgTaskChip in chat looks each task up by id; it lives next
  // to the `mcp__bg__run` tool_use block that started it.
  bgTasks: Record<string, BgTask[]>;
  upsertBgTask: (sessionId: string, task: BgTask) => void;
  setBgTasks: (sessionId: string, tasks: BgTask[]) => void;

  // Agent-to-agent delegations spawned by a parent session.
  // Keyed by parent sessionId → list of delegation records. The
  // request and event cards in chat resolve their live state by
  // looking up the delegation_id in this map.
  // (agent-collaboration.md §6)
  delegations: Record<string, Delegation[]>;
  upsertDelegation: (parentSessionId: string, d: Delegation) => void;
  setDelegations: (parentSessionId: string, ds: Delegation[]) => void;

  // Sidebar visibility toggle for `origin === "delegation"` sessions.
  // Hidden by default so heavy fan-out doesn't flood the agent list;
  // toggle persists in localStorage so the user's preference sticks
  // across reloads.
  showDelegations: boolean;
  setShowDelegations: (v: boolean) => void;

  // Native deep-research jobs, keyed by sessionId → list (native-deep-research.md
  // §7). The ResearchCard renders live phase/progress; the final report arrives
  // as a normal injected turn.
  research: Record<string, ResearchJob[]>;
  upsertResearch: (sessionId: string, job: Partial<ResearchJob> & { id: string }) => void;
  setResearch: (sessionId: string, jobs: ResearchJob[]) => void;
}

export interface ResearchJob {
  id: string;
  session_id: string;
  question: string;
  status: "running" | "completed" | "failed" | "cancelled" | "interrupted";
  phase: string | null;        // scope | search | verify | synthesize | done
  detail?: string;
  counts?: Record<string, number>;
  sources?: string[];
  verified?: number;
  error?: string | null;
}

export interface BgTask {
  id: string;
  session_id: string;
  command: string;
  description: string | null;
  working_dir: string;
  status:
    | "running"
    | "completed"
    | "failed"
    | "cancelled"
    | "interrupted"
    | "pending";
  exit_code: number | null;
  stdout: string;
  stderr: string;
  truncated: boolean;
  started_at: string;
  completed_at: string | null;
}

// Live record of an agent-to-agent delegation, mirrored from
// `GET /api/sessions/{parent}/delegations`. Used by the delegation
// request/event cards in chat and by the sidebar inbound badge.
// (agent-collaboration.md §6)
export interface Delegation {
  delegation_id: string;
  sub_session_id: string;
  parent_session_id: string;
  target_agent_id: string;
  target_agent_name: string;
  request: string;
  state: "running" | "completed" | "failed" | "cancelled";
  created_at: string;
  finished_at: string | null;
  error: string | null;
}

export const useSessionStore = create<SessionStore>((set) => ({
  token: localStorage.getItem("octopus_token") || "",
  setToken: (t) => {
    localStorage.setItem("octopus_token", t);
    set({ token: t });
  },

  agents: [],
  setAgents: (agents) => set({ agents }),
  upsertAgent: (agent) =>
    set((s) => {
      const idx = s.agents.findIndex((a) => a.id === agent.id);
      const agents =
        idx >= 0
          ? [...s.agents.slice(0, idx), agent, ...s.agents.slice(idx + 1)]
          : [...s.agents, agent];
      return { agents };
    }),
  removeAgent: (id) =>
    set((s) => ({ agents: s.agents.filter((a) => a.id !== id) })),
  activeAgentId: null,
  setActiveAgentId: (activeAgentId) => set({ activeAgentId }),

  availableBackends: ["claude-code"],
  setAvailableBackends: (availableBackends) => set({ availableBackends }),

  sessions: [],
  setSessions: (sessions) => set({ sessions }),
  archivedSessions: [],
  setArchivedSessions: (archivedSessions) => set({ archivedSessions }),
  updateSessionStatus: (id, status) =>
    set((s) => ({
      sessions: s.sessions.map((sess) =>
        sess.id === id ? { ...sess, status } : sess
      ),
    })),

  activeSessionId: null,
  setActiveSessionId: (id) => set({ activeSessionId: id }),

  lastAppliedSeq: {},
  setLastAppliedSeq: (sessionId, seq) =>
    set((s) => {
      const current = s.lastAppliedSeq[sessionId] ?? -1;
      if (seq <= current) return s;
      return { lastAppliedSeq: { ...s.lastAppliedSeq, [sessionId]: seq } };
    }),

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

  credentials: [],
  setCredentials: (credentials) => set({ credentials }),

  connectorCatalog: [],
  setConnectorCatalog: (connectorCatalog) => set({ connectorCatalog }),
  connectorInstallations: [],
  setConnectorInstallations: (connectorInstallations) =>
    set({ connectorInstallations }),
  upsertConnectorInstallation: (c) =>
    set((s) => {
      const idx = s.connectorInstallations.findIndex((i) => i.id === c.id);
      const connectorInstallations =
        idx >= 0
          ? [
              ...s.connectorInstallations.slice(0, idx),
              c,
              ...s.connectorInstallations.slice(idx + 1),
            ]
          : [...s.connectorInstallations, c];
      return { connectorInstallations };
    }),
  removeConnectorInstallation: (id) =>
    set((s) => ({
      connectorInstallations: s.connectorInstallations.filter(
        (i) => i.id !== id
      ),
      // Also drop it from every agent's enabled set so the UI stays consistent.
      agentConnectorIds: Object.fromEntries(
        Object.entries(s.agentConnectorIds).map(([aid, ids]) => [
          aid,
          ids.filter((x) => x !== id),
        ])
      ),
    })),
  agentConnectorIds: {},
  setAgentConnectorIds: (agentId, ids) =>
    set((s) => ({
      agentConnectorIds: { ...s.agentConnectorIds, [agentId]: ids },
    })),

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

  pendingForks: {},
  setPendingFork: (sessionId, label) =>
    set((s) => ({
      pendingForks: { ...s.pendingForks, [sessionId]: { label } },
    })),
  clearPendingFork: (sessionId) =>
    set((s) => {
      if (!s.pendingForks[sessionId]) return s;
      const next = { ...s.pendingForks };
      delete next[sessionId];
      return { pendingForks: next };
    }),

  pendingQuestions: {},
  setPendingQuestions: (sessionId, qs) =>
    set((s) => {
      const next = { ...s.pendingQuestions };
      if (qs.length === 0) delete next[sessionId];
      else next[sessionId] = qs;
      return { pendingQuestions: next };
    }),
  addPendingQuestion: (sessionId, q) =>
    set((s) => ({
      pendingQuestions: {
        ...s.pendingQuestions,
        [sessionId]: [...(s.pendingQuestions[sessionId] || []), q],
      },
    })),
  removePendingQuestion: (sessionId, questionId) =>
    set((s) => {
      const cur = s.pendingQuestions[sessionId] || [];
      const filtered = cur.filter((q) => q.question_id !== questionId);
      const next = { ...s.pendingQuestions };
      if (filtered.length === 0) delete next[sessionId];
      else next[sessionId] = filtered;
      return { pendingQuestions: next };
    }),

  connected: false,
  setConnected: (c) => set({ connected: c }),

  viewer: null,
  openViewer: (sessionId, path) => set({ viewer: { sessionId, path } }),
  closeViewer: () => set({ viewer: null }),

  bgTasks: {},
  upsertBgTask: (sessionId, task) =>
    set((s) => {
      const current = s.bgTasks[sessionId] || [];
      const idx = current.findIndex((t) => t.id === task.id);
      const next = idx >= 0
        ? [...current.slice(0, idx), { ...current[idx], ...task }, ...current.slice(idx + 1)]
        : [...current, task];
      return { bgTasks: { ...s.bgTasks, [sessionId]: next } };
    }),
  setBgTasks: (sessionId, tasks) =>
    set((s) => ({ bgTasks: { ...s.bgTasks, [sessionId]: tasks } })),

  research: {},
  upsertResearch: (sessionId, job) =>
    set((s) => {
      const current = s.research[sessionId] || [];
      const idx = current.findIndex((j) => j.id === job.id);
      if (idx < 0) {
        // Inserting a NEW card: only do so from a full payload (started /
        // snapshot). A bare progress/completed/failed patch for an unknown id
        // (missed `research_started` after reconnect or in a 2nd tab) would
        // render a card with no question/status — skip it; the /research
        // snapshot fetch on session load seeds those properly (Vera review).
        if (!job.question || !job.status) return {};
        return { research: { ...s.research, [sessionId]: [...current, job as ResearchJob] } };
      }
      const next = [
        ...current.slice(0, idx),
        { ...current[idx], ...job },
        ...current.slice(idx + 1),
      ];
      return { research: { ...s.research, [sessionId]: next } };
    }),
  setResearch: (sessionId, jobs) =>
    set((s) => ({ research: { ...s.research, [sessionId]: jobs } })),

  delegations: {},
  upsertDelegation: (parentSessionId, d) =>
    set((s) => {
      const current = s.delegations[parentSessionId] || [];
      const idx = current.findIndex((x) => x.delegation_id === d.delegation_id);
      const next =
        idx >= 0
          ? [
              ...current.slice(0, idx),
              { ...current[idx], ...d },
              ...current.slice(idx + 1),
            ]
          : [...current, d];
      return {
        delegations: { ...s.delegations, [parentSessionId]: next },
      };
    }),
  setDelegations: (parentSessionId, ds) =>
    set((s) => ({
      delegations: { ...s.delegations, [parentSessionId]: ds },
    })),

  showDelegations: localStorage.getItem("octopus_show_delegations") === "true",
  setShowDelegations: (v) => {
    if (v) localStorage.setItem("octopus_show_delegations", "true");
    else localStorage.removeItem("octopus_show_delegations");
    set({ showDelegations: v });
  },
}));
