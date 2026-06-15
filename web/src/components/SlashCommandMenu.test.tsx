/**
 * Tests for the slash-command autocomplete: the pure query/filter helpers
 * (which decide when the menu shows and which commands match) plus the
 * rendered menu's selection + hover wiring.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import {
  SLASH_COMMANDS,
  SlashCommandMenu,
  buildRememberPrompt,
  filterSlashCommands,
  slashQuery,
} from "./SlashCommandMenu";

afterEach(cleanup);

describe("slashQuery", () => {
  it("returns the query while typing a command name", () => {
    expect(slashQuery("/")).toBe("/");
    expect(slashQuery("/sch")).toBe("/sch");
    expect(slashQuery("/schedule")).toBe("/schedule");
  });

  it("is null for non-slash input", () => {
    expect(slashQuery("")).toBeNull();
    expect(slashQuery("hello")).toBeNull();
    expect(slashQuery(" /reset")).toBeNull(); // leading space isn't a command
  });

  it("is null once whitespace starts the args (menu should hide)", () => {
    expect(slashQuery("/schedule ")).toBeNull();
    expect(slashQuery("/schedule daily at 9am")).toBeNull();
  });
});

describe("filterSlashCommands", () => {
  it("lists every command for a bare slash", () => {
    expect(filterSlashCommands("/")).toHaveLength(SLASH_COMMANDS.length);
  });

  it("prefix-matches case-insensitively", () => {
    expect(filterSlashCommands("/sch").map((c) => c.name)).toEqual([
      "/schedule",
    ]);
    expect(filterSlashCommands("/SCH").map((c) => c.name)).toEqual([
      "/schedule",
    ]);
    expect(filterSlashCommands("/a").map((c) => c.name)).toEqual(["/archive"]);
    expect(filterSlashCommands("/f").map((c) => c.name)).toEqual(["/fork"]);
  });

  it("matches /remember, including the /re overlap with /reset", () => {
    expect(filterSlashCommands("/rem").map((c) => c.name)).toEqual([
      "/remember",
    ]);
    // /remember, /research, /rewind and /reset share the /re prefix; order
    // preserved (list order).
    expect(filterSlashCommands("/re").map((c) => c.name)).toEqual([
      "/remember",
      "/research",
      "/rewind",
      "/reset",
    ]);
  });

  it("includes /showme and disambiguates against /schedule on the /s prefix", () => {
    expect(filterSlashCommands("/sho").map((c) => c.name)).toEqual([
      "/showme",
    ]);
    // Both /schedule and /showme share the /s prefix; list order is preserved.
    expect(filterSlashCommands("/s").map((c) => c.name)).toEqual([
      "/schedule",
      "/showme",
    ]);
  });

  it("returns nothing for unknown prefixes or non-command input", () => {
    expect(filterSlashCommands("/xyz")).toEqual([]);
    expect(filterSlashCommands("hi")).toEqual([]);
    expect(filterSlashCommands("/schedule now")).toEqual([]);
  });
});

describe("buildRememberPrompt", () => {
  it("embeds the note and points at the shared memory format", () => {
    const prompt = buildRememberPrompt("the user prefers tabs over spaces");
    expect(prompt).toContain("the user prefers tabs over spaces");
    expect(prompt).toContain("long-term memory");
    expect(prompt).toContain("MEMORY.md");
    expect(prompt).toContain("frontmatter");
  });
});

describe("SlashCommandMenu", () => {
  it("renders matches and marks the active item selected", () => {
    render(
      <SlashCommandMenu
        commands={SLASH_COMMANDS}
        activeIndex={1}
        onSelect={() => {}}
        onHoverIndex={() => {}}
      />
    );
    const options = screen.getAllByRole("option");
    expect(options).toHaveLength(SLASH_COMMANDS.length);
    expect(options[1]).toHaveAttribute("aria-selected", "true");
    expect(options[0]).toHaveAttribute("aria-selected", "false");
  });

  it("fires onSelect on click and onHoverIndex on hover", () => {
    const onSelect = vi.fn();
    const onHoverIndex = vi.fn();
    render(
      <SlashCommandMenu
        commands={SLASH_COMMANDS}
        activeIndex={0}
        onSelect={onSelect}
        onHoverIndex={onHoverIndex}
      />
    );
    const archiveIndex = SLASH_COMMANDS.findIndex(
      (c) => c.name === "/archive"
    );
    const archive = screen.getByRole("option", { name: /\/archive/ });
    fireEvent.mouseEnter(archive);
    expect(onHoverIndex).toHaveBeenCalledWith(archiveIndex);
    fireEvent.click(archive);
    expect(onSelect).toHaveBeenCalledWith(SLASH_COMMANDS[archiveIndex]);
  });

  it("renders nothing when there are no commands", () => {
    const { container } = render(
      <SlashCommandMenu
        commands={[]}
        activeIndex={0}
        onSelect={() => {}}
        onHoverIndex={() => {}}
      />
    );
    expect(container.firstChild).toBeNull();
  });
});
