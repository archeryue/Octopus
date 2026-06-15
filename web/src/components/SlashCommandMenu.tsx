import {
  IconArchive,
  IconBrain,
  IconCalendarClock,
  IconCopy,
  IconFile,
  IconGitFork,
  IconRefresh,
  IconWorldSearch,
} from "@tabler/icons-react";
import { cn } from "../lib/utils";

// A slash command surfaced in the composer autocomplete. The `name`
// includes the leading slash so it matches what the user types. Keep this
// list in sync with the command routing in ChatView.handleSend — this is
// the single source of truth for *which* commands exist; handleSend owns
// what they *do*.
export interface SlashCommand {
  name: string;
  // Optional argument hint shown after the name, e.g. "[when] [what]".
  hint?: string;
  description: string;
  Icon: React.ComponentType<{ size?: number; className?: string }>;
}

export const SLASH_COMMANDS: SlashCommand[] = [
  {
    name: "/schedule",
    hint: "[when] [what]",
    description: "Schedule a run — bare opens the overview",
    Icon: IconCalendarClock,
  },
  {
    name: "/remember",
    hint: "<text>",
    description: "Save a note to the agent's long-term memory",
    Icon: IconBrain,
  },
  {
    name: "/research",
    hint: "<question>",
    description: "Deep research — fan-out web search + a cited report",
    Icon: IconWorldSearch,
  },
  {
    name: "/showme",
    hint: "<path>",
    description: "Open a file in the in-app viewer",
    Icon: IconFile,
  },
  {
    name: "/rewind",
    description: "Rewind to a user message and redo it as a new branch",
    Icon: IconGitFork,
  },
  {
    name: "/fork",
    hint: "[name]",
    description: "Fork onto a full copy of the working dir (parent untouched)",
    Icon: IconCopy,
  },
  {
    name: "/archive",
    description: "Archive this session and start fresh",
    Icon: IconArchive,
  },
  {
    name: "/reset",
    description: "Force-reset a stuck session",
    Icon: IconRefresh,
  },
];

/**
 * The turn we inject for `/remember <text>`. Sent as a normal message so the
 * session's agent runs it on whichever harness it uses — both Claude (native
 * memory) and Codex (memory blurb) know the `MEMORY.md` + frontmatter format,
 * so one instruction works for both. Kept pure + exported for testing.
 */
export function buildRememberPrompt(text: string): string {
  return (
    "Remember this for future sessions. Save it to your long-term memory: " +
    "create or update the appropriate memory file (with YAML frontmatter) and " +
    "the MEMORY.md index, deduping against anything already stored. Then " +
    "confirm in one line what you saved — don't do anything else.\n\n" +
    `To remember: ${text}`
  );
}

/**
 * The slash query if the input is in "typing a command" state — it starts
 * with "/" and hasn't reached a space yet (once there's whitespace the user
 * has moved on to the command's arguments, so the menu should hide).
 * Returns `null` otherwise.
 */
export function slashQuery(input: string): string | null {
  if (!input.startsWith("/")) return null;
  if (/\s/.test(input)) return null;
  return input;
}

/** Commands whose name starts with the current query (case-insensitive). */
export function filterSlashCommands(input: string): SlashCommand[] {
  const q = slashQuery(input);
  if (q === null) return [];
  const lower = q.toLowerCase();
  return SLASH_COMMANDS.filter((c) => c.name.startsWith(lower));
}

interface Props {
  commands: SlashCommand[];
  activeIndex: number;
  onSelect: (cmd: SlashCommand) => void;
  onHoverIndex: (index: number) => void;
  className?: string;
}

export function SlashCommandMenu({
  commands,
  activeIndex,
  onSelect,
  onHoverIndex,
  className,
}: Props) {
  if (commands.length === 0) return null;
  return (
    <div
      className={cn(
        "slash-menu z-30 overflow-hidden rounded-xl border-[0.7px] border-gray-400 bg-card p-1 shadow-lg",
        className
      )}
      role="listbox"
      aria-label="Slash commands"
    >
      {commands.map((cmd, i) => {
        const active = i === activeIndex;
        return (
          <button
            key={cmd.name}
            type="button"
            role="option"
            aria-selected={active}
            data-slash-command={cmd.name}
            // Selecting must not steal focus from the textarea — preventing
            // the mousedown default keeps the caret where it was so we can
            // refocus + reposition after the click handler runs.
            onMouseDown={(e) => e.preventDefault()}
            onMouseEnter={() => onHoverIndex(i)}
            onClick={() => onSelect(cmd)}
            className={cn(
              "slash-item flex w-full items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-left text-sm transition-colors",
              active
                ? "bg-accent text-foreground"
                : "text-foreground hover:bg-accent/60"
            )}
          >
            <cmd.Icon size={15} className="shrink-0 text-muted-foreground" />
            <span className="font-mono font-medium">{cmd.name}</span>
            {cmd.hint && (
              <span className="font-mono text-xs text-muted-foreground">
                {cmd.hint}
              </span>
            )}
            <span className="ml-auto truncate pl-3 text-xs text-muted-foreground">
              {cmd.description}
            </span>
          </button>
        );
      })}
    </div>
  );
}
