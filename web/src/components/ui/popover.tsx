import { useEffect, useRef, type ReactNode } from "react";

/** A small panel anchored under its trigger.
 *
 * The console already has radix's DropdownMenu, and that is the right thing
 * for a *menu*. This is for the other case: a panel with a form in it. A
 * dropdown menu owns the keyboard — typeahead, item focus, arrow keys — and
 * all of that fights a textarea living inside it.
 *
 * Controlled on purpose: the owner usually has to know whether the panel is
 * open anyway (to close it after a successful submit), so hiding that state
 * in here would only mean handing a ref back out.
 */
export function Popover({
  open,
  onClose,
  trigger,
  children,
  className = "",
  panelClassName = "",
  label,
}: {
  open: boolean;
  onClose: () => void;
  trigger: ReactNode;
  children: ReactNode;
  className?: string;
  panelClassName?: string;
  /** Accessible name for the panel. */
  label?: string;
}) {
  const wrap = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: MouseEvent) => {
      if (!wrap.current?.contains(e.target as Node)) onClose();
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    // mousedown, not click: a click that starts outside and ends inside (a
    // drag-select that overshoots the panel) shouldn't be treated as "done".
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open, onClose]);

  return (
    <div className={`popover-wrap relative ${className}`} ref={wrap}>
      {trigger}
      {open && (
        <div
          role="dialog"
          aria-label={label}
          className={`popover-panel absolute right-0 top-[calc(100%+7px)] z-50 rounded-xl border-[0.7px] border-gray-400 bg-card p-3 shadow-[0_18px_44px_-22px_rgba(28,44,72,0.4)] ${panelClassName}`}
        >
          {children}
        </div>
      )}
    </div>
  );
}
