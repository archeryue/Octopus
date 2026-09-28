import { useState, type ReactNode } from "react";
import {
  IconPin,
  IconPinnedFilled,
  IconSearch,
} from "@tabler/icons-react";

/** One agent or application as the All tab lists it. */
export interface LibraryItem {
  id: string;
  name: string;
  description: string;
  icon: ReactNode;
  /** A short mono line under the name: backend, session count, status. */
  meta: ReactNode;
  pinned: boolean;
  archived: boolean;
  /** Pinned and not allowed to change — the Default Agent. */
  pinLocked?: boolean;
}

/** The All tab of the Agents and Applications pages (sidebar-pins.md §6).
 *
 * Every item, in three sections: **In sidebar** (the pinned ones, in the
 * sidebar's order), **Not in sidebar**, and **Archived**. The pin toggle is
 * the one control every live row has; everything else a row can do comes
 * from `actions`, because an agent and an application do different things.
 * A section with nothing in it is left out, and the filter narrows all three
 * at once by name and description.
 */
export function ItemLibrary({
  noun,
  items,
  onTogglePin,
  actions,
  empty,
}: {
  /** Plural, lower-case: "agents", "applications". */
  noun: string;
  /** Live items (pinned ones already in sidebar order), then archived. */
  items: LibraryItem[];
  onTogglePin: (item: LibraryItem, pinned: boolean) => void;
  actions: (item: LibraryItem) => ReactNode;
  /** Shown when there is nothing at all. */
  empty: ReactNode;
}) {
  const [query, setQuery] = useState("");

  if (items.length === 0) {
    return <div className="library-empty card px-6 py-12 text-center">{empty}</div>;
  }

  const q = query.trim().toLowerCase();
  const matching = q
    ? items.filter(
        (i) =>
          i.name.toLowerCase().includes(q) ||
          i.description.toLowerCase().includes(q)
      )
    : items;

  const sections: { key: string; title: string; hint: string; rows: LibraryItem[] }[] = [
    {
      key: "pinned",
      title: "In sidebar",
      hint: "drag them in the sidebar to reorder",
      rows: matching.filter((i) => !i.archived && i.pinned),
    },
    {
      key: "unpinned",
      title: "Not in sidebar",
      hint: "still live — pin one to add a shortcut",
      rows: matching.filter((i) => !i.archived && !i.pinned),
    },
    {
      key: "archived",
      title: "Archived",
      hint: "out of use until restored",
      rows: matching.filter((i) => i.archived),
    },
  ];

  return (
    <div className="item-library space-y-6">
      <label className="library-filter flex items-center gap-2 rounded-xl border border-gray-400 bg-card px-3.5 py-2 focus-within:border-primary focus-within:ring-[3px] focus-within:ring-primary/10">
        <IconSearch size={15} className="shrink-0 text-gray-600" aria-hidden />
        <input
          className="library-filter-input min-w-0 flex-1 bg-transparent text-[13.5px] text-gray-900 outline-none placeholder:text-gray-600"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={`Filter ${items.length} ${noun}`}
          aria-label={`Filter ${noun}`}
        />
      </label>

      {matching.length === 0 && (
        <p className="library-no-match px-1 text-[13px] text-gray-700">
          No {noun} match “{query.trim()}”.
        </p>
      )}

      {sections
        .filter((s) => s.rows.length > 0)
        .map((s) => (
          <section key={s.key} className={`library-section library-${s.key}`}>
            <h3 className="mb-2 flex items-baseline gap-2 px-1">
              <span className="font-mono text-[11px] uppercase tracking-[0.12em] text-gray-700">
                {s.title}
              </span>
              <span className="library-count font-mono text-[11px] text-gray-600">
                {s.rows.length}
              </span>
              <span className="ml-auto hidden text-[12px] text-gray-600 md:inline">
                {s.hint}
              </span>
            </h3>
            <ul className="card divide-y divide-gray-300 overflow-hidden">
              {s.rows.map((item) => (
                <LibraryRow
                  key={item.id}
                  item={item}
                  onTogglePin={onTogglePin}
                  actions={actions(item)}
                />
              ))}
            </ul>
          </section>
        ))}
    </div>
  );
}

function LibraryRow({
  item,
  onTogglePin,
  actions,
}: {
  item: LibraryItem;
  onTogglePin: (item: LibraryItem, pinned: boolean) => void;
  actions: ReactNode;
}) {
  return (
    <li
      className={`library-row flex items-center gap-3 px-4 py-3${
        item.archived ? " archived" : ""
      }`}
      data-id={item.id}
    >
      <span className={`shrink-0${item.archived ? " opacity-60" : ""}`}>
        {item.icon}
      </span>
      <span className="min-w-0 flex-1">
        <span className="library-name block truncate text-[14px] font-semibold text-gray-950">
          {item.name}
        </span>
        {item.description && (
          <span className="library-description block truncate text-[12.5px] text-gray-800">
            {item.description}
          </span>
        )}
        <span className="library-meta mt-0.5 block truncate font-mono text-[10.5px] text-gray-700">
          {item.meta}
        </span>
      </span>
      <span className="library-actions flex shrink-0 items-center gap-1.5">
        {actions}
        {!item.archived && <PinToggle item={item} onTogglePin={onTogglePin} />}
      </span>
    </li>
  );
}

function PinToggle({
  item,
  onTogglePin,
}: {
  item: LibraryItem;
  onTogglePin: (item: LibraryItem, pinned: boolean) => void;
}) {
  if (item.pinLocked) {
    return (
      <span
        className="btn-library-pin locked inline-flex size-8 items-center justify-center text-gray-500"
        title="Always in the sidebar"
        aria-label={`${item.name} is always in the sidebar`}
      >
        <IconPinnedFilled size={16} />
      </span>
    );
  }
  return (
    <button
      type="button"
      className={`btn-library-pin inline-flex size-8 items-center justify-center rounded-lg transition-colors ${
        item.pinned
          ? "pinned text-primary hover:bg-primary-50"
          : "text-gray-600 hover:bg-gray-100 hover:text-gray-900"
      }`}
      onClick={() => onTogglePin(item, !item.pinned)}
      aria-pressed={item.pinned}
      title={item.pinned ? "Unpin from sidebar" : "Pin to sidebar"}
      aria-label={
        item.pinned
          ? `Unpin ${item.name} from the sidebar`
          : `Pin ${item.name} to the sidebar`
      }
    >
      {item.pinned ? <IconPinnedFilled size={16} /> : <IconPin size={16} />}
    </button>
  );
}
