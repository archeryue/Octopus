/**
 * The backend readout. A backend that won't start is the failure mode this
 * feature has to answer for, so the panel's job is to say what state it's in
 * and hand over the log — not to look tidy.
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

  it("shows the port and uptime while running", () => {
    const { container } = render(
      <BackendPanel backend={backend({ state: "running", port: 4321, uptime_s: 90 })} />
    );
    expect(container.textContent).toContain("running");
    expect(container.textContent).toContain("port 4321");
    expect(container.textContent).toContain("2m");
  });

  it("shows why a backend failed", () => {
    const { container } = render(
      <BackendPanel
        backend={backend({ state: "failed", error: "start.sh exited 7" })}
      />
    );
    expect(container.textContent).toContain("failed");
    expect(container.textContent).toContain("start.sh exited 7");
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
