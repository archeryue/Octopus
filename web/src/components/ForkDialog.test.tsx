/**
 * Renderer tests for the fork-confirm view (session-tree-rewind.md §5.6.2).
 * Drives the presentational `ForkConfirmView` directly so we don't have to
 * mock the two-step fetch flow: it must render the three-class side-effect
 * summary and gate the single revert checkbox on the preflight result.
 */

import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { ForkConfirmView, type ForkPreview } from "./ForkDialog";

afterEach(cleanup);

function makePreview(overrides: Partial<ForkPreview> = {}): ForkPreview {
  return {
    rewind_to_msg_seq: 2,
    prefilled_prompt: "redo this",
    side_effect_summary: {
      file_edits: [
        { path: "server/auth.py", turns: 3 },
        { path: "server/db.py", turns: 1 },
      ],
      bg_tasks: [
        { task_id: "t1", command: "bun run test:e2e", description: null, status: "running" },
      ],
      other_tools: [
        { label: "Bash commands", count: 12 },
        { label: "github calls", count: 2 },
      ],
      counts: { total: 17, file_edits: 2, bg_tasks: 1 },
    },
    revert: { available: true, refused_reason: null },
    can_fork: true,
    ...overrides,
  };
}

function renderView(preview: ForkPreview, revertChecked = true) {
  return render(
    <ForkConfirmView
      parentName="Refactor auth"
      preview={preview}
      revertChecked={revertChecked}
      onRevertChange={() => {}}
      label=""
      onLabelChange={() => {}}
    />
  );
}

describe("ForkConfirmView", () => {
  it("renders the three-class side-effect summary", () => {
    renderView(makePreview());
    expect(screen.getByText("Files modified (2)")).toBeTruthy();
    expect(screen.getByText("server/auth.py")).toBeTruthy();
    expect(screen.getByText("Background tasks (1)")).toBeTruthy();
    expect(screen.getByText("bun run test:e2e")).toBeTruthy();
    expect(screen.getByText(/Other tool activity/)).toBeTruthy();
    expect(screen.getByText("12 Bash commands")).toBeTruthy();
  });

  it("enables the revert checkbox when the preflight allows it", () => {
    renderView(makePreview());
    const cb = screen.getByTestId("fork-revert-checkbox") as HTMLInputElement;
    expect(cb.disabled).toBe(false);
    expect(cb.checked).toBe(true);
  });

  it("disables the revert checkbox with the refusal reason when unavailable", () => {
    renderView(
      makePreview({
        revert: {
          available: false,
          refused_reason: "HEAD has moved since the fork-point",
        },
      }),
      false
    );
    const cb = screen.getByTestId("fork-revert-checkbox") as HTMLInputElement;
    expect(cb.disabled).toBe(true);
    // The reason is surfaced as a tooltip on the label and inline.
    const label = screen.getByTestId("fork-revert-label");
    expect(label.getAttribute("title")).toContain("HEAD has moved");
    expect(
      screen.getByTestId("fork-revert-reason").textContent
    ).toContain("HEAD has moved");
  });

  it("handles an empty file-edit list", () => {
    renderView(
      makePreview({
        side_effect_summary: {
          file_edits: [],
          bg_tasks: [],
          other_tools: [],
          counts: { total: 0, file_edits: 0, bg_tasks: 0 },
        },
      })
    );
    expect(screen.getByText("Files modified (0)")).toBeTruthy();
    expect(screen.getByText("None")).toBeTruthy();
  });
});
