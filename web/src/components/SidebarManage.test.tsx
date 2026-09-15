/**
 * The MANAGE group is the app's ambient health readout — the whole reason the
 * console design puts counts and a dot on every row. These tests cover what
 * that summary says and where each row navigates.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { SidebarManage } from "./SidebarManage";
import {
  useSessionStore,
  type ConnectorInstallationInfo,
  type CredentialInfo,
  type Schedule,
} from "../stores/sessionStore";

function schedule(overrides: Partial<Schedule> = {}): Schedule {
  return {
    id: "sch1",
    agent_id: "ag1",
    name: "Release checkup",
    prompt: "check the release",
    interval_seconds: null,
    cron: "0 9 * * *",
    timezone: "Asia/Shanghai",
    recurrence_label: "Daily 09:00",
    enabled: true,
    created_at: "2026-01-01T00:00:00Z",
    last_run_at: null,
    origin_session_id: null,
    run_at: null,
    next_run_at: null,
    ...overrides,
  } as Schedule;
}

const credential = (o: Partial<CredentialInfo> = {}) =>
  ({
    id: "c1",
    label: "archer-cc",
    backend: "claude-code",
    auth_type: "oauth",
    status: "active",
    needs_reconnect: false,
    ...o,
  }) as CredentialInfo;

const installation = (o: Partial<ConnectorInstallationInfo> = {}) =>
  ({
    id: "i1",
    kind: "github",
    label: "GitHub",
    auth_type: "oauth",
    needs_reconnect: false,
    ...o,
  }) as ConnectorInstallationInfo;

beforeEach(() => {
  useSessionStore.setState({
    token: "tok",
    mainView: "chat",
    schedules: [],
    credentials: [],
    connectorInstallations: [],
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response("[]", { status: 200 }))
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("SidebarManage", () => {
  it("navigates to each manage page", () => {
    render(<SidebarManage />);
    fireEvent.click(screen.getByText("Schedules"));
    expect(useSessionStore.getState().mainView).toBe("schedules");
    fireEvent.click(screen.getByText("Connectors"));
    expect(useSessionStore.getState().mainView).toBe("connectors");
    fireEvent.click(screen.getByText("Harness"));
    expect(useSessionStore.getState().mainView).toBe("harness");
  });

  it("shows the next fire time next to the schedule count", async () => {
    const soon = new Date();
    soon.setHours(soon.getHours() + 1, 30, 0, 0);
    useSessionStore.setState({
      schedules: [schedule({ next_run_at: soon.toISOString() })],
    });
    const { container } = render(<SidebarManage />);
    await waitFor(() =>
      expect(container.querySelector(".btn-manage-schedules")?.textContent).toMatch(
        /next \d{2}:\d{2}/
      )
    );
  });

  it("ignores a disabled schedule when working out what's next", () => {
    const soon = new Date(Date.now() + 3600_000).toISOString();
    useSessionStore.setState({
      schedules: [schedule({ enabled: false, next_run_at: soon })],
    });
    const { container } = render(<SidebarManage />);
    expect(
      container.querySelector(".btn-manage-schedules")?.textContent
    ).not.toMatch(/next/);
  });

  it("turns the connector summary amber when one needs reconnecting", () => {
    useSessionStore.setState({
      connectorInstallations: [
        installation(),
        installation({ id: "i2", label: "Wiki", needs_reconnect: true }),
      ],
    });
    const { container } = render(<SidebarManage />);
    const row = container.querySelector(".btn-manage-connectors");
    expect(row?.textContent).toContain("needs reconnect");
    expect(row?.querySelector(".bg-warn")).toBeTruthy();
  });

  it("counts healthy connectors when all are fine", () => {
    useSessionStore.setState({
      connectorInstallations: [installation(), installation({ id: "i2" })],
    });
    const { container } = render(<SidebarManage />);
    const row = container.querySelector(".btn-manage-connectors");
    expect(row?.textContent).toContain("2");
    expect(row?.querySelector(".bg-success")).toBeTruthy();
  });

  it("flags a lapsed engine credential the same way", () => {
    useSessionStore.setState({
      credentials: [credential({ needs_reconnect: true })],
    });
    const { container } = render(<SidebarManage />);
    const row = container.querySelector(".btn-manage-harness");
    expect(row?.textContent).toContain("needs reconnect");
    expect(row?.querySelector(".bg-warn")).toBeTruthy();
  });

  it("marks the open page as active", () => {
    useSessionStore.setState({ mainView: "harness" });
    const { container } = render(<SidebarManage />);
    expect(container.querySelector(".btn-manage-harness.active")).toBeTruthy();
    expect(container.querySelector(".btn-manage-schedules.active")).toBeNull();
  });
});
