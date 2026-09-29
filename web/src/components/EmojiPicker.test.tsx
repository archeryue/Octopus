/**
 * The avatar picker restored what the console redesign dropped: choosing an
 * avatar by clicking, not by hunting for an OS emoji keyboard.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { EmojiPicker } from "./EmojiPicker";

afterEach(cleanup);

describe("EmojiPicker", () => {
  it("shows the fallback until an emoji is chosen", () => {
    render(<EmojiPicker value="" onChange={() => {}} fallback="N" />);
    expect(screen.getByRole("button", { name: "Choose an avatar" }).textContent).toBe("N");
  });

  it("shows the current emoji when set", () => {
    render(<EmojiPicker value="🐰" onChange={() => {}} fallback="N" />);
    expect(screen.getByRole("button", { name: "Choose an avatar" }).textContent).toBe("🐰");
  });

  it("the grid is hidden until the tile is clicked", () => {
    render(<EmojiPicker value="" onChange={() => {}} fallback="N" />);
    expect(screen.queryByLabelText("Use 🐰")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Choose an avatar" }));
    expect(screen.getByLabelText("Use 🐰")).toBeTruthy();
  });

  it("clicking an emoji reports it and closes", () => {
    const onChange = vi.fn();
    render(<EmojiPicker value="" onChange={onChange} fallback="N" />);
    fireEvent.click(screen.getByRole("button", { name: "Choose an avatar" }));
    fireEvent.click(screen.getByLabelText("Use 🦊"));
    expect(onChange).toHaveBeenCalledWith("🦊");
    // Panel closed → grid gone.
    expect(screen.queryByLabelText("Use 🦊")).toBeNull();
  });

  it("still accepts a custom emoji pasted into the field", () => {
    const onChange = vi.fn();
    render(<EmojiPicker value="" onChange={onChange} fallback="N" />);
    fireEvent.click(screen.getByRole("button", { name: "Choose an avatar" }));
    fireEvent.change(screen.getByLabelText("Custom avatar"), { target: { value: "🎨" } });
    expect(onChange).toHaveBeenCalledWith("🎨");
  });

  it("clears the avatar", () => {
    const onChange = vi.fn();
    render(<EmojiPicker value="🐰" onChange={onChange} fallback="N" />);
    fireEvent.click(screen.getByRole("button", { name: "Choose an avatar" }));
    fireEvent.click(screen.getByText("clear"));
    expect(onChange).toHaveBeenCalledWith("");
  });
});
