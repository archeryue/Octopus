/**
 * The backend readout. A backend that won't start is the failure mode this
 * feature has to answer for, so the control's job is to say what state it's in
 * and hand over the log — not to look tidy.
 *
 * It's a header control now (app-agent-access.md §7): the state word is
 * always on screen, the detail and the log are behind it.
 */

import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render } from "@testing-library/react";

import { BackendPanel } from "./BackendPanel";

afterEach(cleanup);

const backend = (o: Record<string, unknown> = {}) =>
  ({ state: "absent", port: null, error: null, uptime_s: null, log_tail: [], ...o }) as never;

describe("BackendPanel", () => {
  it("renders nothing for an application without a backend", () => {
    // A static app's UI must be exactly as it was.
    const { container } = render(<BackendPanel backend={backend()} />);
    expect(container.firstChild).toBeNull();
  });

  it("shows the state without being asked, and the port behind it", () => {
    const { container } = render(
      <BackendPanel backend={backend({ state: "running", port: 4321, uptime_s: 90 })} />
    );
    // Always visible — it's the part you glance at.
    expect(container.querySelector(".backend-state")?.textContent).toBe("running");
    expect(container.textContent).not.toContain("port 4321");

    fireEvent.click(container.querySelector(".backend-summary")!);
    expect(container.textContent).toContain("port 4321");
    expect(container.textContent).toContain("2m");
  });

  it("shows why a backend failed", () => {
    const { container } = render(
      <BackendPanel
        backend={backend({ state: "failed", error: "start.sh exited 7" })}
      />
    );
    // A failure is visible at a glance; the reason is one click away.
    expect(container.textContent).toContain("failed");
    fireEvent.click(container.querySelector(".backend-summary")!);
    expect(container.textContent).toContain("start.sh exited 7");
  });

  it("closes on Escape", () => {
    const { container } = render(
      <BackendPanel backend={backend({ state: "running", port: 1, log_tail: ["x"] })} />
    );
    fireEvent.click(container.querySelector(".backend-summary")!);
    expect(container.querySelector(".backend-log")).toBeTruthy();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(container.querySelector(".backend-log")).toBeNull();
  });

  it("keeps the log one click away rather than on screen", () => {
    const { container } = render(
      <BackendPanel
        backend={backend({ state: "failed", error: "boom", log_tail: ["line one", "line two"] })}
      />
    );
    expect(container.querySelector(".backend-log")).toBeNull();
    fireEvent.click(container.querySelector(".backend-summary")!);
    expect(container.querySelector(".backend-log")?.textContent).toContain("line two");
  });

  it("offers no expander when there is nothing logged", () => {
    const { container } = render(
      <BackendPanel backend={backend({ state: "stopped" })} />
    );
    fireEvent.click(container.querySelector(".backend-summary")!);
    expect(container.querySelector(".backend-log")).toBeNull();
  });
});
