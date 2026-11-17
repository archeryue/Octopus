# Chat scroll: open at the newest message, and stay there

> **Status:** planned (2026-11-16). Nothing here is built yet.

Opening a session often leaves the view somewhere in the middle of the
conversation instead of at its newest message — most often in long sessions,
on every device. This plan records the cause as measured, and the fix.

---

## 1. The cause

A session is "parked" at its newest message once, when it opens
(`ChatView.tsx`, the park effect: `scrollToIndex("LAST")` re-issued for three
frames). After that, nothing keeps it there.

- The browser measures scroll position **from the top**. Parking sets it to
  "total height minus viewport". When something near the bottom then grows,
  the position stays the same distance from the top, and the bottom moves away
  from the screen.
- react-virtuoso (4.18.6) follows growth back to the bottom only in a 100 ms
  window after the item count changes (`followOutput`'s `SIZE_INCREASED`
  handler unsubscribes after `setTimeout(…, 100)`). Growth after that window
  is not followed.

Three things in a transcript grow after it is parked:

| Content | How it grows |
|---|---|
| Images: attachment thumbnails, images in markdown | No reserved size; a thumbnail is 0 px tall until it has downloaded |
| bg-task chips | The placeholder ("Loading bg task…") is one row; the chip, once its record is fetched, adds a second row with the last line of output |
| Delegation cards | A placeholder until the delegation record arrives (polled every 2 s), then an extra line |

Long sessions contain many of them, so the effect is frequent; every one that
grows after parking pushes the newest message further below the screen.

**Measured** (Playwright, sandbox, 2026-11-16): four images arriving 800 ms
after open left the view 900 px above the bottom, permanently. The same
transcript without late content parked at 0 px.

**Ruled out** by the same measurements, in a phone-sized touch viewport with
the CPU slowed 4×, on a 300-message transcript of long markdown, code and
tables — every case parked within 1 px of the bottom:

- first open, and reopening a session already in the store;
- the WebSocket dropping and reconnecting, which replaces the active
  session's transcript (what a backgrounded phone or tablet tab goes through);
- paging back to older messages, returning to the end, then reconnecting.

Length and device are therefore not causes in themselves; they multiply the
late-growing content. Not covered by the measurements: real iOS Safari (only
Chromium is installed on the box), and messages that arrive while a tab is
away; §9 closes both.

---

## 2. The rule

**While the reader is at the bottom, the conversation is anchored to its
bottom.** Anything that changes its height — a new message, streamed text, an
image arriving, a card filling in — keeps the newest content on screen. Once
the reader scrolls up, nothing moves the view until they come back.

This is what every chat product does, and it replaces three mechanisms that
each did part of the job: the three-frame park, Virtuoso's `followOutput`, and
its 100 ms growth window.

Reserving the size of late content (§5) is the second half: it keeps the view
still for a reader who has scrolled up, where the bottom anchor does not apply.

---

## 3. Stick to bottom

### 3.1 The state

A pure module, `web/src/lib/stickToBottom.ts`, holds the decision; `ChatView`
feeds it events and performs what it returns. Pure so that every transition is
unit-tested without a browser.

State: `{ stuck: boolean, settled: boolean, unseen: number }`.

| Event | Effect |
|---|---|
| Session opened (active id changes) | `stuck = true`, `settled = false`, `unseen = 0` |
| Content height changed | If `stuck` and the gap to the bottom is over 1 px: scroll to the bottom |
| Reached the bottom (gap ≤ threshold) | `stuck = true`, `unseen = 0`; the first time after open, `settled = true` |
| The reader scrolled away from the bottom (§3.2) | `stuck = false` |
| Message appended while not stuck | `unseen += 1` |
| The reader sends a message | `stuck = true`, scroll to the bottom |
| "Latest" button pressed (§6) | `stuck = true`, scroll to the bottom |

The threshold is Virtuoso's `atBottomThreshold`, set explicitly (8 px) rather
than left at its default, so the list and the module agree on what "at the
bottom" means.

### 3.2 Telling the reader's scroll from ours

Only the reader can unstick the view. A scroll caused by content growing, by
Virtuoso correcting an estimated item size, or by our own scroll-to-bottom must
never count — otherwise a correction above the viewport (which moves
`scrollTop`) would unstick a reader who never touched anything.

So unsticking needs **intent**. The scroller listens for the reader's input:

- `wheel` with `deltaY < 0`;
- touch: a `touchmove` whose finger moves down (the content scrolls up);
- keys, while the list has focus: `PageUp`, `ArrowUp`, `Home`, `Shift+Space`;
- `pointerdown` on the scroller, which covers dragging the scrollbar.

Intent is held while a touch or pointer is down, and for 250 ms after a wheel
or key event. A `scroll` event during intent that leaves the gap above the
threshold unsticks; any other `scroll` event changes nothing.

### 3.3 Observing height

One `ResizeObserver` on the element that holds the list's full height — the
first child of Virtuoso's scroller (taken from `scrollerRef`) — **and** one on
the footer, because streamed text and the running indicator live in the footer
(`ChatView`'s `footer`), outside the item list. Both feed "content height
changed".

Scrolling to the bottom sets `scroller.scrollTop = scroller.scrollHeight`
directly. The first scroll lands on estimated sizes; Virtuoso then renders and
measures the last items, the height changes, the observer fires, and the next
scroll lands on real sizes. That loop converges in a few frames and is what
replaces the fixed three-frame park.

### 3.4 What it replaces in `ChatView`

- The park effect and its frame counter go.
- `followOutput` goes (`false`): following is the observer's job, and
  Virtuoso's smooth follow would compete with it.
- `initialTopMostItemIndex` stays: on a remount with messages already in the
  store it avoids one frame at the top.
- `parked`, which arms paging back, becomes `settled`: paging is armed once the
  view has reached the bottom after opening, the same guarantee as today.

---

## 4. A snapshot never removes what is held before its window

`applyTranscript` replaces a session's messages with the snapshot. When older
pages had been loaded (paging back), the replacement removes them from the
front of the list while `firstItemIndex` still accounts for them, so Virtuoso's
indices no longer match the data. The measurements did not catch a visible
jump from this, but the state is inconsistent and the next prepend computes
from it.

The snapshot is authoritative for its own range and says nothing about what
lies before it. So `applyTranscript` **merges**:

- keep the held messages with `seq` below the snapshot's `oldest_loaded_seq`;
- take the snapshot for everything from there on;
- keep the held `oldestSeq` and `hasMoreMessages` when held messages were kept,
  since they describe the older end.

The front of the list then only ever changes through `prependMessages`, which
already adjusts `firstItemIndex` in the same tick. It applies to all three
callers: opening a session, the reconnect refetch, and the archived viewer.

---

## 5. Reserve the size of what arrives late

### 5.1 Attachment images

The server records an image's dimensions when it is uploaded, and the
thumbnail renders at its final size before a byte has arrived.

- `AttachmentMetadata` gains `width` and `height` (optional ints). It is stored
  as JSON on the message row, so there is no schema change.
- `server/attachments.py` reads them from the file header — PNG (`IHDR`), JPEG
  (`SOFn`), GIF, WebP (`VP8`, `VP8L`, `VP8X`) — with a stdlib parser rather
  than a new dependency for four headers.
- **JPEG orientation:** browsers apply EXIF orientation (`image-orientation:
  from-image` is the default), so orientations 5–8 swap width and height. Phone
  photos are exactly this case.
- Anything unparsed (HEIC, SVG, a truncated file) has no dimensions and falls
  back to a fixed box (below).
- **Existing attachments:** a backfill at startup fills in the dimensions of
  every image attachment already referenced by a message, read from its file
  on disk. It lives in `server/attachments.py` and is called from the
  lifespan, not among the `_migrate_*` steps in `server/db/base.py`, because it
  reads files and those steps only touch the database. Idempotent: it skips
  entries that already have dimensions, and leaves missing files alone.
- `MessageBubble`'s thumbnail computes its box from the dimensions and the
  existing limits (160 px tall, 16 rem wide, contained) and sets it as an
  explicit `width`/`height`. Without dimensions it renders a fixed 160 px-tall
  box with the image contained in it.

### 5.2 Images in markdown

An agent's markdown image has no known size. `MessageBubble` gives the markdown
renderer an `img` component that places it in a fixed-height frame (240 px,
full width, `object-fit: contain`), opening the full image on click. Today such
an image renders at its natural size, which can be anything.

### 5.3 bg-task chips

The placeholder takes the shape of the collapsed chip: the header row **and**
the output row, the second one empty. The "Waiting for bg task to register…"
placeholder takes the same shape. Expanding a chip remains a height change, but
one the reader caused.

### 5.4 Delegation cards

The placeholder renders the card's full layout — name, status, request line
and the reply line — with the values it does not have yet left blank.

---

## 6. A way back to the newest

When the view is not stuck, a "Latest ↓" button floats above the composer,
with the count of messages that arrived since the reader left the bottom
(`unseen`). Pressing it scrolls to the bottom and sticks. It disappears once
the view is stuck again. On a phone it is a 44 px touch target (`mobile.md`).

---

## 7. Open with 100 messages, not 200

`MESSAGE_WINDOW` drops from 200 to 100. The window exists to make opening fast
(`polish-2026-09.md` §4 B2), and with long tool output a message is often tens
of kilobytes. Rendering cost does not depend on it — the list renders only what
is on screen — but transfer, parsing and the store do. Paging back fetches the
same size, so reading older history is unchanged apart from one more fetch per
100 messages.

The value is declared twice today: `MESSAGE_WINDOW` in `routers/sessions.py`
and a copied `_ARCHIVED_MESSAGE_WINDOW` in `sessions/lifecycle.py`. It moves to
one constant in `server/models.py`, next to the response models that describe
the window, and both import it.

---

## 8. Files

| File | Change |
|---|---|
| `web/src/lib/stickToBottom.ts` | New: the state module of §3.1 |
| `web/src/components/ChatView.tsx` | Intent listeners, the two observers, the button; the park effect and `followOutput` removed; `parked` → `settled` |
| `web/src/lib/transcript.ts` | `applyTranscript` merges (§4) |
| `web/src/components/MessageBubble.tsx` | Thumbnail box from dimensions; markdown `img` frame |
| `web/src/components/BgTaskChip.tsx` | Placeholders in the collapsed chip's shape |
| `web/src/components/AgentDelegationRequestCard.tsx` | Placeholder in the card's shape |
| `server/attachments.py` | Header parser; dimensions on upload; the startup backfill |
| `server/models.py` | `AttachmentMetadata.width`/`height`; `MESSAGE_WINDOW = 100` |
| `server/routers/sessions.py`, `server/sessions/lifecycle.py` | Import the one constant |
| `server/main.py` | Call the backfill in the lifespan |

---

## 9. Tests

**Frontend unit (vitest)**

- `stickToBottom`: every transition in §3.1, including that a height change
  while not stuck does nothing, and that a scroll without intent never
  unsticks.
- `applyTranscript`: a snapshot keeps held messages older than its window and
  keeps the older-end cursor; with nothing older held it replaces as today.
- Thumbnail box: computed size from dimensions, for landscape, portrait and
  oversized images, and the fixed box without dimensions.

**Backend (pytest)**

- The header parser on PNG, JPEG (each EXIF orientation), GIF and the three
  WebP variants, plus truncated and non-image input (no dimensions, no error).
- An upload records dimensions; a non-image does not.
- The backfill fills existing rows, is idempotent, and skips missing files.
- Opening returns 100 messages, and a page back returns the 100 before them.

**E2E (Playwright), `web/e2e/chat-scroll.spec.ts`**

Cases 1–7 need no model and run in the fast bucket. Case 8 runs a real turn —
a message can only be sent over the socket, and sending starts one — so it
sits in a describe titled `@llm`.

Each case asserts the gap to the bottom (`scrollHeight − scrollTop −
clientHeight`), not that the last message is "visible":

1. Images that arrive late (a route delayed by 800 ms): the gap is within 2 px
   after they load.
2. bg-task chips whose records arrive late (the `bg-tasks` route delayed): the
   same.
3. Streamed text growing in the footer keeps the view at the bottom.
4. Scrolled up, then late content grows below: the message at the top of the
   screen does not move.
5. Scrolled up: the "Latest" button appears with its count; pressing it
   returns to the bottom and it disappears.
6. The socket drops and reconnects while at the bottom, and while scrolled back
   through older pages: at the bottom it stays there; scrolled back, the
   message on screen stays on screen and the older pages are still loaded.
7. Touch: a phone viewport (`isMobile`, `hasTouch`), a downward finger drag
   unsticks, and content growth afterwards does not move the view.
8. Messages that arrive while the tab is away: with the first page's socket
   closed, a second page sends to the same session; after the first page
   reconnects, the new message and its reply are at its bottom.

Cases 1, 5 and 7 also run under **WebKit** in a mobile-Safari profile, since
the iPhone and iPad are where this is read most. That means installing
Playwright's WebKit on the box and adding the profile to
`playwright.config.ts` for this spec.

The two existing scroll tests (`new-features.spec.ts`: "opens at the newest…"
and "renders long conversation…pins to bottom") change their "last message is
visible" assertion to the same gap assertion.

**On the user's devices**, after shipping: a long real session opened on the
iPhone, the iPad and the PC, each landing at the newest message.

---

## What this defers

- **Restoring the last reading position per session.** Some products reopen a
  conversation where it was left rather than at the newest message. Octopus
  keeps opening at the newest; changing that is a product decision, not part of
  this fix.
- **`VirtuosoMessageList`**, react-virtuoso's commercially licensed chat list,
  which anchors to the bottom natively. §3 gives the same behaviour on the free
  list.
