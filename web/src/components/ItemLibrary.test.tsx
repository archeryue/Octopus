/** The All tab's list (sidebar-pins.md §6): sections, the pin control, the
 * filter. The agent- and application-specific actions are tested on their
 * pages. */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { ItemLibrary, type LibraryItem } from "./ItemLibrary";

const item = (o: Partial<LibraryItem>): LibraryItem => ({
  id: "i",
  name: "Item",
  description: "",
  icon: null,
  meta: "",
  pinned: true,
  archived: false,
  ...o,
});

afterEach(cleanup);

function mount(items: LibraryItem[], onTogglePin = vi.fn()) {
  const utils = render(
    <ItemLibrary
      noun="agents"
      items={items}
      onTogglePin={onTogglePin}
      actions={() => null}
      empty={<p>Nothing here.</p>}
    />
  );
  return { ...utils, onTogglePin };
}

describe("ItemLibrary", () => {
  it("leaves out an empty section", () => {
    const { container } = mount([item({ id: "a" })]);
    expect(container.querySelector(".library-pinned")).toBeTruthy();
    expect(container.querySelector(".library-unpinned")).toBeNull();
    expect(container.querySelector(".library-archived")).toBeNull();
  });

  it("toggles a pin, but not a locked one or an archived one", () => {
    const { onTogglePin } = mount([
      item({ id: "a", name: "Pinned" }),
      item({ id: "b", name: "Loose", pinned: false }),
      item({ id: "c", name: "Locked", pinLocked: true }),
      item({ id: "d", name: "Gone", archived: true }),
    ]);
    fireEvent.click(screen.getByLabelText("Unpin Pinned from the sidebar"));
    fireEvent.click(screen.getByLabelText("Pin Loose to the sidebar"));
    expect(onTogglePin.mock.calls.map(([i, p]) => [i.id, p])).toEqual([
      ["a", false],
      ["b", true],
    ]);
    expect(screen.getByLabelText("Locked is always in the sidebar")).toBeTruthy();
    expect(screen.queryByLabelText(/Gone (to|from) the sidebar/)).toBeNull();
  });

  it("says when the filter matches nothing", () => {
    mount([item({ id: "a", name: "Vera" })]);
    fireEvent.change(screen.getByLabelText("Filter agents"), {
      target: { value: "zzz" },
    });
    expect(screen.getByText(/No agents match/)).toBeTruthy();
  });

  it("shows the empty state when there is nothing at all", () => {
    mount([]);
    expect(screen.getByText("Nothing here.")).toBeTruthy();
    expect(screen.queryByLabelText("Filter agents")).toBeNull();
  });
});
