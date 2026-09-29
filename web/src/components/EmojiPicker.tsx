import { useState } from "react";
import { IconX } from "@tabler/icons-react";

import { Popover } from "./ui/popover";

/** Pick an agent/app avatar by clicking, not by hunting for an OS emoji
 * keyboard.
 *
 * The avatar has always been "type an emoji into a text box", which on a
 * machine without an easy emoji key means it can't be set at all — an early
 * version had a picker and it was lost in the console redesign. This puts a
 * clickable grid back, and keeps a small free-text field for anything not in
 * it, so a custom emoji is still one paste away.
 *
 * A curated set, not an emoji database: a couple of rows of friendly, legible
 * glyphs cover what an avatar is for (a face for the agent) far better than a
 * searchable 3,000-emoji panel, and it stays a plain grid with no dependency.
 */
const CHOICES: string[] = [
  "🐙", "🦊", "🐰", "🐱", "🐶", "🐼", "🐨", "🐵",
  "🦉", "🦋", "🐝", "🐢", "🐬", "🦄", "🐥", "🦔",
  "🌸", "🌷", "🌻", "🍀", "⭐", "✨", "🌈", "🔥",
  "🧠", "🔬", "🩺", "🛠️", "📊", "📚", "✏️", "🧭",
  "🚀", "🛰️", "💡", "🎯", "🔍", "🗂️", "⚙️", "🤖",
  "😀", "😎", "🤓", "🥳", "👩‍💻", "👨‍💻", "🧑‍🔬", "🦾",
];

export function EmojiPicker({
  value,
  onChange,
  fallback,
}: {
  value: string;
  /** Called with the chosen emoji, or "" when cleared. */
  onChange: (emoji: string) => void;
  /** Shown on the tile when no emoji is set (e.g. the name's first letter). */
  fallback: string;
}) {
  const [open, setOpen] = useState(false);

  return (
    <Popover
      open={open}
      onClose={() => setOpen(false)}
      label="Choose an avatar"
      panelClassName="w-[19rem]"
      trigger={
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-label="Choose an avatar"
          aria-expanded={open}
          className="avatar-picker-trigger tile size-14 rounded-xl bg-primary text-2xl text-white transition-transform hover:scale-[1.04] focus:outline-none focus:ring-[3px] focus:ring-primary/25"
        >
          {value || fallback}
        </button>
      }
    >
      <div className="space-y-2.5">
        <div className="grid grid-cols-8 gap-1">
          {CHOICES.map((emoji) => (
            <button
              key={emoji}
              type="button"
              aria-label={`Use ${emoji}`}
              className={`emoji-choice flex size-8 items-center justify-center rounded-lg text-xl transition-colors hover:bg-gray-100 ${
                value === emoji ? "bg-primary-100 ring-1 ring-primary" : ""
              }`}
              onClick={() => {
                onChange(emoji);
                setOpen(false);
              }}
            >
              {emoji}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2 border-t border-gray-200 pt-2.5">
          <input
            className="avatar-custom-input w-16 rounded-lg border border-gray-400 bg-card px-2 py-1 text-center text-base outline-none focus:border-primary"
            value={value}
            onChange={(e) => onChange(e.target.value)}
            placeholder="🐙"
            maxLength={8}
            aria-label="Custom avatar"
          />
          <span className="min-w-0 flex-1 truncate text-[11.5px] text-gray-600">
            or paste your own
          </span>
          {value && (
            <button
              type="button"
              className="avatar-clear inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-[11.5px] text-gray-700 hover:bg-gray-100"
              onClick={() => onChange("")}
            >
              <IconX size={13} /> clear
            </button>
          )}
        </div>
      </div>
    </Popover>
  );
}
