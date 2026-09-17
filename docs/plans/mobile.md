# Octopus on a phone

> **Implementation status: SHIPPED.** The drawer in the store
> (`sessionStore.sidebarOpen` + every navigation action), the mobile/touch
> layer at the end of `web/src/index.css`, the header changes in `PageHeader`
> / `ChatView` / `ApplicationView`, and the composer's touch behaviour.
> Verified at 320, 390, 412, 810 and 844×390 with zero horizontal overflow.

## 1. Why this is not "responsive CSS"

Octopus is used from a phone as much as from a laptop, often on the *same*
session — you start something at the desk and check on it from a bus. So the
phone isn't a degraded desktop: it's the second primary surface, and the
things that break on it break the product, not the layout.

Three different triggers, deliberately kept apart, because conflating them is
how a "mobile fix" breaks a desktop with a touchscreen:

| trigger | what it governs |
|---|---|
| `max-width: 768px` | **layout** — the sidebar becomes a drawer, headers shed context |
| `hover: none` | **input** — hit areas, and affordances that were hidden behind hover |
| `env(safe-area-inset-*)` | **hardware** — the notch and the home indicator |

## 2. The drawer has to put itself away

The worst bug found on a phone: picking a session left the drawer open, so
every navigation ended with the thing you'd just chosen hidden behind the menu
you'd chosen it from. You had to tap the dimmed strip to see your own choice.

The fix isn't in the sidebar components — it's in the store. `sidebarOpen`
moved out of `App`'s local state and into `sessionStore`, and every action
that changes what the main pane shows (`setActiveSessionId`, `openApplication`,
`openApplicationCreate`, `openAgentForm`, `openManage`, `showChat`) closes it
in the same `set()`. There is no call site that can forget, including ones
written later. Desktop ignores the flag entirely.

The drawer is `min(270px, 84vw)` so a 320px phone keeps a strip of the app
visible — the affordance that says "tap here to dismiss" — and its closed
position is derived from that width rather than hard-coded. The old rule had
`left: -260px` against a 270px sidebar, which left a 10px sliver of the
sidebar on screen at all times.

## 3. Typing

* **Fields are 16px on phones.** iOS zooms the page whenever a focused field
  is smaller, and never zooms back out. This is the one rule in the file that
  needs `!important`: the sizes come from utility classes on dozens of
  fields, and this is a platform behaviour to defeat, not a style to prefer.
* **Enter is a newline on touch, not send.** There is no Shift+Enter on a
  phone keyboard, so a return key that sends makes a second paragraph
  impossible to type. The send button is under your thumb anyway, and
  `enterkeyhint` matches whichever behaviour is live.
* The token field on the login screen turns off autocapitalize, autocorrect
  and spellcheck — it's a secret, not prose.

## 4. Headers shed context, not function

A 390px header can't hold a breadcrumb chain, a status pill, a credential
chip, a connection pill and four icon buttons. What goes, in order:

1. **Leading crumbs** (`Applications / OctoFeature` → `OctoFeature`). You just
   tapped the thing; you have the context. Without this the *name* is what
   truncates — to a letter and a slash.
2. **Words that a dot already says.** The connection pill keeps its coloured
   dot and drops "Connected".
3. **Long labels with short equivalents.** "Built by Octo" becomes "Ready";
   "Building" stays "Building", because that one is why you're looking.
4. **The engine-credential chip**, below 640px only. It names a credential and
   opens a picker, neither of which fits; it's a desk-bound action and it
   reappears on anything wider.

Everything that *does* something is still on the header. Nothing moved into a
hidden menu.

## 5. Touch targets and hover-only affordances

Delete buttons and the per-agent "+" were `opacity-0` until hover — invisible
on a touchscreen, where there is no hover, only a tap that has already chosen
something. Under `hover: none` they're simply visible.

Hit areas grow past the button's box with a pseudo-element (`inset: -10px`)
rather than by resizing the button: the console's density is deliberate, and
an 18px icon with a 38px target is the combination that keeps both. Rows get
`:active` backgrounds since `:hover` never fires, and everything tappable gets
`touch-action: manipulation` so a tap isn't held for 300ms waiting to see
whether it was a double-tap zoom.

## 6. Notch, home indicator, status bar

`viewport-fit=cover` lets the page use the whole screen, which also means it
extends under the hardware. `.app-layout` pays back the top and side insets;
the bottom inset is paid by the two surfaces that sit on that edge (the
composer and the sidebar's account bar) so they still extend under the home
indicator instead of leaving a dead strip above it.

The iOS status bar style changed from `black-translucent` to `default`.
Translucent means iOS draws the clock and battery *over* the page in white,
and Octopus's header is white — they disappeared.

These compose with the visual-viewport hook (`--app-h` / `--app-top`, which
tracks the keyboard): insets are hardware, the visual viewport is software,
and both are needed.

The hook does **not** simply size the app to the visual viewport, because that
is only trustworthy while something is being typed into. After the keyboard
animates away iOS can leave a stale, smaller height behind, and an app sized
from it stops short of the bottom of the screen — a band of dead page under
the composer. The rule is: nothing editable focused → the app is exactly
`innerHeight` tall, whatever the visual viewport claims; editing → follow the
visual viewport exactly. Focus and orientation changes re-measure a few times
over the next 650ms (the resize that matters is the one at the *end* of the
keyboard animation, which iOS doesn't reliably send), and a tap anywhere
re-measures as a last resort for a keyboard dismissed while the field kept
focus.

The bottom inset uses `max()` rather than adding to the bar's own padding: 34pt
of home indicator already clears the edge, and stacking both lifts the composer
off the bottom for no reason.

## 7. Long strings

Paths, ids and URLs are everywhere in this app, and grid/flex children size
to their longest unbreakable word — one absolute path in a schedule's prompt
was enough to widen a column past the screen. Page bodies (and the schedule
prompt at every width) break instead.

## 8. Testing

E2E at phone width: the drawer closes when a session is picked, the closed
drawer is entirely off-screen, no surface scrolls horizontally, and every
field is at least 16px. Unit: the store's navigation actions close the drawer.
Manual verification: 320 / 390 / 412 / 810 / 844×390, zero overflow on chat,
applications, schedules, connectors, harness, the agent form and dialogs.

## 9. What this defers

* **Gestures.** A left-edge swipe to open the drawer collides with iOS
  Safari's back-swipe, and a custom drag would have to beat the browser's own
  handling to be reliable. The hamburger and the dimmed overlay are
  unambiguous.
* **A phone-specific information architecture** (bottom tab bar, per-surface
  back stack). The drawer plus a single main pane is coherent on both sizes;
  a second navigation model would be a second thing to keep correct.
* **Offline / service worker.** Octopus is a client for a machine you own; it
  is useless offline, and a stale-cached shell would be worse than the error.
