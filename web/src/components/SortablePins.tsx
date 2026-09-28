import type { CSSProperties, HTMLAttributes, ReactNode } from "react";
import {
  DndContext,
  KeyboardSensor,
  MouseSensor,
  TouchSensor,
  closestCenter,
  useSensor,
  useSensors,
  type Announcements,
  type DragEndEvent,
  type UniqueIdentifier,
} from "@dnd-kit/core";
import {
  SortableContext,
  arrayMove,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";

/** Drag-to-reorder for the sidebar's pinned rows (sidebar-pins.md §3).
 *
 * The sensors are chosen so dragging never costs a row its other gestures:
 * the mouse has to travel 6px before a press becomes a drag, so a click still
 * opens the row; a finger has to hold for 250ms, so a swipe still scrolls the
 * drawer; and the keyboard sensor (Space to lift, arrows to move, Space to
 * drop) makes the order reachable without a pointer at all.
 *
 * What a screen reader hears is said in names and places ("Vera moved to
 * position 2 of 5") — the library's default speaks the internal ids.
 */
export function SortablePins({
  ids,
  nameOf,
  onReorder,
  children,
}: {
  ids: string[];
  /** The row's name, for what a screen reader announces. */
  nameOf: (id: string) => string;
  /** The full pinned order after a drop that changed it. */
  onReorder: (orderedIds: string[]) => void;
  children: ReactNode;
}) {
  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 6 } }),
    useSensor(TouchSensor, {
      activationConstraint: { delay: 250, tolerance: 5 },
    }),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    })
  );

  const onDragEnd = ({ active, over }: DragEndEvent) => {
    if (!over || active.id === over.id) return;
    const from = ids.indexOf(String(active.id));
    const to = ids.indexOf(String(over.id));
    if (from < 0 || to < 0) return;
    onReorder(arrayMove(ids, from, to));
  };

  const place = (id: UniqueIdentifier | undefined) =>
    `position ${ids.indexOf(String(id)) + 1} of ${ids.length}`;
  const announcements: Announcements = {
    onDragStart: ({ active }) =>
      `Picked up ${nameOf(String(active.id))}, at ${place(active.id)}.`,
    // Silent over its own place: that fires the moment a row is lifted, and
    // saying "moved to position 3" for a row already at 3 both talks over
    // "Picked up" and tells the listener nothing.
    onDragOver: ({ active, over }) =>
      !over
        ? `${nameOf(String(active.id))} is not over a position.`
        : over.id === active.id
          ? undefined
          : `${nameOf(String(active.id))} moved to ${place(over.id)}.`,
    onDragEnd: ({ active, over }) =>
      over
        ? `${nameOf(String(active.id))} dropped at ${place(over.id)}.`
        : `${nameOf(String(active.id))} dropped where it was.`,
    onDragCancel: ({ active }) =>
      `Moving ${nameOf(String(active.id))} cancelled; it stays where it was.`,
  };

  return (
    <DndContext
      sensors={sensors}
      collisionDetection={closestCenter}
      onDragEnd={onDragEnd}
      accessibility={{
        announcements,
        screenReaderInstructions: {
          draggable:
            "To reorder, press Space to pick this up, the arrow keys to " +
            "move it, and Space again to drop it. Escape cancels.",
        },
      }}
    >
      <SortableContext items={ids} strategy={verticalListSortingStrategy}>
        {children}
      </SortableContext>
    </DndContext>
  );
}

/** What a sortable row hands its drag handle — spread onto the element that
 * should start a drag (the agent row, not the sessions nested under it). */
export type DragHandle = HTMLAttributes<HTMLElement>;

/** One pinned row. `children` receives the handle props; the wrapper owns
 * the movement. Vertical only: the sidebar is a column, and letting a row
 * wander sideways while it's carried reads as broken. */
export function SortablePin({
  id,
  className,
  children,
}: {
  id: string;
  className?: string;
  children: (handle: DragHandle) => ReactNode;
}) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id });
  const style: CSSProperties = {
    transform: CSS.Translate.toString(transform ? { ...transform, x: 0 } : null),
    transition,
    position: "relative",
    zIndex: isDragging ? 10 : undefined,
  };
  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`${className ?? ""}${isDragging ? " dragging" : ""}`}
    >
      {children({ ...attributes, ...listeners } as DragHandle)}
    </div>
  );
}
