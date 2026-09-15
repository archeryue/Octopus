/**
 * Renderer tests for the application main pane (applications.md §7): the
 * ready / building / failed branches, the change-request composer, reload,
 * and the auth cookie the iframe depends on.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { ApplicationView } from "./ApplicationView";
import { useSessionStore, type Application } from "../stores/sessionStore";

function application(overrides: Partial<Application> = {}): Application {
  return {
    id: "a1",
    name: "Habit Tracker",
    description: "Track habits",
    icon: "✅",
    agent_id: "ag1",
    session_id: "s1",
    app_dir: "/tmp/apps/habit-tracker",
    entrypoint: "index.html",
    status: "ready",
    error: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    last_built_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

const agent = {
  id: "ag1",
  name: "Octo",
  avatar: "🐙",
} as unknown as ReturnType<typeof useSessionStore.getState>["agents"][number];

let fetchMock: ReturnType<typeof vi.fn>;
// jsdom honours cookie path scoping, so a cookie written with `path=/apps`
// is invisible to `document.cookie` on a page served from `/` — exactly the
// scoping we want in production. Capture the writes instead.
let cookieWrites: string[];
let restoreCookie: (() => void) | null = null;

function mount(app: Application) {
  useSessionStore.setState({
    token: "tok",
    agents: [agent],
    applications: [app],
    activeApplicationId: app.id,
    mainView: "application",
  });
  return render(<ApplicationView onToggleSidebar={() => {}} />);
}

beforeEach(() => {
  fetchMock = vi.fn(
    async () =>
      new Response(JSON.stringify(application()), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })
  );
  vi.stubGlobal("fetch", fetchMock);

  cookieWrites = [];
  const proto = Object.getPrototypeOf(document);
  const original = Object.getOwnPropertyDescriptor(proto, "cookie")!;
  Object.defineProperty(document, "cookie", {
    configurable: true,
    get: () => original.get!.call(document),
    set: (v: string) => {
      cookieWrites.push(v);
      original.set!.call(document, v);
    },
  });
  restoreCookie = () => {
    delete (document as unknown as Record<string, unknown>).cookie;
    restoreCookie = null;
  };
});

afterEach(() => {
  cleanup();
  restoreCookie?.();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("ApplicationView", () => {
  it("renders the app in an iframe when ready", () => {
    const { container } = mount(application());
    const frame = container.querySelector("iframe.application-frame");
    expect(frame).toBeTruthy();
    expect(frame!.getAttribute("src")).toContain("/apps/a1/");
    // Without allow-same-origin the app's localStorage throws; without
    // withholding allow-top-navigation it could navigate Octopus away.
    const sandbox = frame!.getAttribute("sandbox") || "";
    expect(sandbox).toContain("allow-same-origin");
    expect(sandbox).toContain("allow-scripts");
    expect(sandbox).not.toContain("allow-top-navigation");
  });

  it("publishes the app cookie so the frame authenticates", () => {
    mount(application());
    const written = cookieWrites.find((c) => c.startsWith("octopus_app_token="));
    expect(written).toBeTruthy();
    expect(written).toContain("octopus_app_token=tok");
    // Scoped to /apps so it never rides along with API or SPA requests.
    expect(written).toContain("path=/apps");
  });

  it("shows the builder while building, not a frame", () => {
    const { container } = mount(application({ status: "building" }));
    expect(container.querySelector("iframe")).toBeNull();
    expect(screen.getByText(/Octo is building Habit Tracker/)).toBeTruthy();
  });

  it("shows the failure reason when the build produced no page", () => {
    const { container } = mount(
      application({ status: "failed", error: "index.html was never written" })
    );
    expect(container.querySelector("iframe")).toBeNull();
    expect(screen.getByText("index.html was never written")).toBeTruthy();
  });

  it("reload re-navigates the frame", () => {
    const { container } = mount(application());
    const before = container
      .querySelector("iframe.application-frame")!
      .getAttribute("src");
    fireEvent.click(screen.getByLabelText("Reload the app"));
    const after = container
      .querySelector("iframe.application-frame")!
      .getAttribute("src");
    expect(after).not.toEqual(before);
  });

  it("a finished rebuild swaps the frame without a manual reload", () => {
    const { container, rerender } = mount(application());
    const before = container
      .querySelector("iframe.application-frame")!
      .getAttribute("src");
    useSessionStore.getState().upsertApplication(
      application({ last_built_at: "2026-02-02T00:00:00Z" })
    );
    rerender(<ApplicationView onToggleSidebar={() => {}} />);
    expect(
      container.querySelector("iframe.application-frame")!.getAttribute("src")
    ).not.toEqual(before);
  });

  it("a change request POSTs to the build route and clears the box", async () => {
    mount(application());
    const box = screen.getByPlaceholderText(/Ask Octo for a change/);
    fireEvent.change(box, { target: { value: "add a dark mode" } });
    fireEvent.click(screen.getByLabelText("Send the change request"));

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([u, init]) => {
          const body = (init as RequestInit | undefined)?.body;
          return (
            String(u).endsWith("/api/applications/a1/build") &&
            typeof body === "string" &&
            JSON.parse(body).prompt === "add a dark mode"
          );
        })
      ).toBe(true)
    );
    await waitFor(() => expect((box as HTMLTextAreaElement).value).toBe(""));
  });

  it("Enter sends, Shift+Enter doesn't", async () => {
    mount(application());
    const box = screen.getByPlaceholderText(/Ask Octo for a change/);

    fireEvent.change(box, { target: { value: "tweak it" } });
    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    expect(
      fetchMock.mock.calls.some(([u]) => String(u).includes("/build"))
    ).toBe(false);

    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([u]) => String(u).includes("/build"))
      ).toBe(true)
    );
  });

  it("surfaces a build-request failure", async () => {
    fetchMock.mockImplementation(
      async () =>
        new Response(JSON.stringify({ detail: "agent is gone" }), {
          status: 409,
          headers: { "Content-Type": "application/json" },
        })
    );
    mount(application());
    fireEvent.change(screen.getByPlaceholderText(/Ask Octo for a change/), {
      target: { value: "fix it" },
    });
    fireEvent.click(screen.getByLabelText("Send the change request"));
    await waitFor(() => expect(screen.getByText("agent is gone")).toBeTruthy());
  });

  it("opening the build session switches the pane back to chat", async () => {
    mount(application());
    fireEvent.click(screen.getByTitle("Open the build session with Octo"));
    await waitFor(() =>
      expect(useSessionStore.getState().activeSessionId).toBe("s1")
    );
    expect(useSessionStore.getState().mainView).toBe("chat");
  });

  it("opening the build session adds it to the sidebar list", async () => {
    // The build session was created server-side, so the sidebar list doesn't
    // have it — without adopting it here the chat header can't name it.
    fetchMock.mockImplementation(async (url: unknown) =>
      String(url).includes("/api/sessions/s1") &&
      !String(url).includes("bg-tasks")
        ? new Response(
            JSON.stringify({
              id: "s1",
              name: "Build: Habit Tracker",
              working_dir: "/tmp/apps/habit-tracker",
              status: "idle",
              origin: "application",
              agent_id: "ag1",
              messages: [{ role: "user", type: "text", content: "brief" }],
              pending_queue: [],
              pending_questions: [],
              next_message_seq: 1,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } }
          )
        : new Response("[]", {
            status: 200,
            headers: { "Content-Type": "application/json" },
          })
    );
    useSessionStore.setState({ sessions: [] });
    mount(application());
    fireEvent.click(screen.getByTitle("Open the build session with Octo"));

    await waitFor(() =>
      expect(useSessionStore.getState().sessions.map((s) => s.id)).toEqual(["s1"])
    );
    const added = useSessionStore.getState().sessions[0] as Record<string, unknown>;
    expect(added.name).toBe("Build: Habit Tracker");
    // Detail-only fields must not leak into the list shape.
    expect(added.messages).toBeUndefined();
    expect(added.next_message_seq).toBeUndefined();
    expect(useSessionStore.getState().messages["s1"]).toHaveLength(1);
  });

  it("degrades gracefully when the application is gone", () => {
    useSessionStore.setState({
      applications: [],
      activeApplicationId: "ghost",
      mainView: "application",
    });
    render(<ApplicationView onToggleSidebar={() => {}} />);
    expect(screen.getByText(/no longer available/)).toBeTruthy();
  });
});
