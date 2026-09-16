/**
 * The sidebar's fold handle.
 *
 * The button is deliberately invisible until its edge is hovered (CSS, not
 * React), so what's tested here is the part that isn't CSS: which direction
 * the chevron points, what the button claims to do, and that the preference
 * outlives a reload.
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { SidebarEdgeToggle } from "./SidebarEdgeToggle";
import { useSessionStore } from "../stores/sessionStore";

beforeEach(() => {
  localStorage.removeItem("octopus_sidebar_collapsed");
  useSessionStore.setState({ sidebarCollapsed: false });
});

afterEach(() => {
  cleanup();
  localStorage.removeItem("octopus_sidebar_collapsed");
});

describe("SidebarEdgeToggle", () => {
  it("offers to collapse while the sidebar is open, and points inward", () => {
    const { container } = render(<SidebarEdgeToggle />);
    const btn = screen.getByLabelText("Collapse sidebar");

    expect(btn.getAttribute("aria-expanded")).toBe("true");
    // The arrow points at the sidebar it would pull in.
    expect(container.querySelector(".tabler-icon-chevron-left")).toBeTruthy();
    expect(container.querySelector(".tabler-icon-chevron-right")).toBeNull();
  });

  it("collapses on click and flips to an outward arrow", () => {
    const { container } = render(<SidebarEdgeToggle />);
    fireEvent.click(screen.getByLabelText("Collapse sidebar"));

    expect(useSessionStore.getState().sidebarCollapsed).toBe(true);
    const btn = screen.getByLabelText("Expand sidebar");
    expect(btn.getAttribute("aria-expanded")).toBe("false");
    expect(container.querySelector(".tabler-icon-chevron-right")).toBeTruthy();

    fireEvent.click(btn);
    expect(useSessionStore.getState().sidebarCollapsed).toBe(false);
    expect(screen.getByLabelText("Collapse sidebar")).toBeTruthy();
  });

  it("persists the choice — folding is a preference, not a navigation step", () => {
    render(<SidebarEdgeToggle />);
    fireEvent.click(screen.getByLabelText("Collapse sidebar"));
    expect(localStorage.getItem("octopus_sidebar_collapsed")).toBe("true");

    fireEvent.click(screen.getByLabelText("Expand sidebar"));
    expect(localStorage.getItem("octopus_sidebar_collapsed")).toBeNull();
  });
});
