import { useMemo, useState } from "react";

import { primeAppCookie } from "../api/applications";
import { useSessionStore, type Application } from "../stores/sessionStore";
import { cn } from "../lib/utils";

const FALLBACK = "🪟";

/** An Application's icon, in the one place that decides what it is.
 *
 * Three sources, in order:
 *
 *   1. `icon` — an emoji the user typed. Theirs, so it always wins.
 *   2. `icon_src` — the app's own icon, discovered from its files after each
 *      build. Either a path inside the app directory, or a `data:` URI the
 *      page inlined.
 *   3. `🪟` — the fallback, which is what every generated app looked like
 *      before this existed.
 *
 * A file is served through `/apps/{id}/…`, versioned by `last_built_at` so a
 * rebuild busts the cache immediately (the server sends a short max-age for
 * exactly this file — without that it re-downloads on every render and
 * flickers).
 *
 * Rendered with `<img>`, never inlined into the DOM: an SVG's scripts don't
 * execute through an `img` tag, and an app's icon is content we didn't write.
 */
export function AppIcon({
  app,
  size = "sm",
  className,
}: {
  app: Pick<Application, "id" | "icon" | "icon_src" | "last_built_at">;
  size?: "sm" | "lg";
  className?: string;
}) {
  const token = useSessionStore((s) => s.token);
  const [failed, setFailed] = useState(false);
  const tile = cn("tile tile-plain", size === "lg" && "tile-lg", className);

  const isFile = !app.icon && !!app.icon_src && !app.icon_src.startsWith("data:");

  // A file icon is fetched from `/apps/{id}/…`, which authenticates with the
  // app cookie — an <img> can't carry an Authorization header any more than an
  // iframe can (applications.md §3). The sidebar shows icons before any app
  // has been opened, so priming can't be left to ApplicationView. A memo is
  // the one hook that runs during render, i.e. before the browser starts the
  // image request; the write is idempotent.
  useMemo(() => {
    if (isFile && token) primeAppCookie(token);
  }, [isFile, token]);

  if (app.icon) {
    return <span className={tile}>{app.icon}</span>;
  }

  // A broken or unreadable icon falls back to the glyph rather than leaving a
  // torn-image placeholder in the sidebar.
  if (app.icon_src && !failed) {
    const src = app.icon_src.startsWith("data:")
      ? app.icon_src
      : `/apps/${app.id}/${app.icon_src}?v=${encodeURIComponent(
          app.last_built_at ?? "",
        )}`;
    return (
      <img
        className={cn(tile, "tile-img")}
        src={src}
        alt=""
        aria-hidden
        onError={() => setFailed(true)}
      />
    );
  }

  return <span className={tile}>{FALLBACK}</span>;
}
