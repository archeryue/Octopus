/**
 * The icon an Application shows, and the order the three sources win in.
 * Before this existed every generated app rendered the same 🪟 even when its
 * own UI was fully branded.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render } from "@testing-library/react";

import { AppIcon } from "./AppIcon";
import { useSessionStore } from "../stores/sessionStore";

// Records what the component asked to be primed, which is the behaviour under
// test — see the cookie-path note in the test below.
const primed: string[] = [];
vi.mock("../api/applications", () => ({
  primeAppCookie: (t: string) => primed.push(t),
}));

beforeEach(() => {
  primed.length = 0;
});

afterEach(cleanup);

const app = (o: Record<string, unknown> = {}) =>
  ({
    id: "a1",
    icon: null,
    icon_src: null,
    last_built_at: "2026-09-15T10:00:00Z",
    ...o,
  }) as Parameters<typeof AppIcon>[0]["app"];

describe("AppIcon", () => {
  it("shows the user's emoji when they set one", () => {
    const { container } = render(<AppIcon app={app({ icon: "💠" })} />);
    expect(container.textContent).toBe("💠");
    expect(container.querySelector("img")).toBeNull();
  });

  it("prefers the user's emoji over the app's own icon", () => {
    // `icon` is the user's, `icon_src` is the app's. A rebuild discovers the
    // second and must never override the first.
    const { container } = render(
      <AppIcon app={app({ icon: "💠", icon_src: "icon.svg" })} />
    );
    expect(container.textContent).toBe("💠");
    expect(container.querySelector("img")).toBeNull();
  });

  it("serves a discovered file through the app's own URL, versioned", () => {
    const { container } = render(
      <AppIcon app={app({ icon_src: "assets/mark.svg" })} />
    );
    const img = container.querySelector("img");
    expect(img?.getAttribute("src")).toBe(
      "/apps/a1/assets/mark.svg?v=2026-09-15T10%3A00%3A00Z"
    );
  });

  it("uses an inline data URI verbatim", () => {
    const uri = "data:image/svg+xml,%3Csvg%3E%3C/svg%3E";
    const { container } = render(<AppIcon app={app({ icon_src: uri })} />);
    expect(container.querySelector("img")?.getAttribute("src")).toBe(uri);
  });

  it("renders the icon as an image, never as inline markup", () => {
    // An app's icon is content we didn't write; an <img> can't execute a
    // script inside an SVG, inlined markup can.
    const { container } = render(
      <AppIcon app={app({ icon_src: "icon.svg" })} />
    );
    expect(container.querySelector("img")).not.toBeNull();
    expect(container.querySelector("svg")).toBeNull();
  });

  it("falls back to the window glyph when there is neither", () => {
    const { container } = render(<AppIcon app={app()} />);
    expect(container.textContent).toBe("🪟");
  });

  it("takes the large tile size when asked", () => {
    const { container } = render(<AppIcon app={app()} size="lg" />);
    expect(container.firstElementChild?.className).toContain("tile-lg");
  });
});

describe("AppIcon failure and auth", () => {
  it("falls back to the glyph when the image fails to load", () => {
    // A torn-image placeholder in the sidebar is worse than the fallback it
    // replaced — this is what a 401 or a deleted file looks like.
    const { container } = render(<AppIcon app={app({ icon_src: "gone.svg" })} />);
    const img = container.querySelector("img")!;
    fireEvent.error(img);
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toBe("🪟");
  });

  it("primes the app cookie for a file icon, since an <img> can't send a header", () => {
    // Asserted on the call rather than document.cookie: the cookie is scoped
    // to `path=/apps`, and jsdom only exposes cookies matching the current
    // path, so reading it back from "/" would be empty either way.
    useSessionStore.setState({ token: "tok-123" });
    render(<AppIcon app={app({ icon_src: "icon.svg" })} />);
    expect(primed).toEqual(["tok-123"]);
  });

  it("does not prime anything for an inline icon", () => {
    // No request is made at all, so there is nothing to authenticate.
    useSessionStore.setState({ token: "tok-123" });
    const { container } = render(
      <AppIcon app={app({ icon_src: "data:image/svg+xml,%3Csvg%3E%3C/svg%3E" })} />
    );
    expect(container.querySelector("img")?.getAttribute("src")).toContain("data:");
    expect(primed).toEqual([]);
  });

  it("does not prime anything for an emoji", () => {
    useSessionStore.setState({ token: "tok-123" });
    render(<AppIcon app={app({ icon: "💠", icon_src: "icon.svg" })} />);
    expect(primed).toEqual([]);
  });
});
