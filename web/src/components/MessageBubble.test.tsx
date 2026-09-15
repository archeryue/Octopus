/**
 * Streaming text renders as plain text, not markdown.
 *
 * Re-parsing a growing string through remark-gfm + remark-math on every 50ms
 * flush is O(n²) work on a long answer and janks the main thread; half-written
 * markdown also renders broken while it arrives (an unclosed code fence, a lone
 * table row). The completed block that replaces it renders normally.
 */

import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render } from "@testing-library/react";

import { MessageBubble } from "./MessageBubble";

afterEach(cleanup);

const streaming = "# Heading\n\n```js\nconst x = 1;";

describe("MessageBubble plain mode", () => {
  it("renders streaming text verbatim, without parsing markdown", () => {
    const { container } = render(
      <MessageBubble
        message={{ role: "assistant", type: "text", content: streaming }}
        sessionId="s1"
        plain
      />
    );
    const el = container.querySelector(".msg-streaming-text");
    expect(el).toBeTruthy();
    // The raw characters are on screen — no <h1>, no half-open <pre>.
    expect(el?.textContent).toBe(streaming);
    expect(container.querySelector("h1")).toBeNull();
  });

  it("renders the completed block as markdown", () => {
    const { container } = render(
      <MessageBubble
        message={{ role: "assistant", type: "text", content: "# Heading" }}
        sessionId="s1"
      />
    );
    expect(container.querySelector(".msg-streaming-text")).toBeNull();
    expect(container.querySelector("h1")?.textContent).toBe("Heading");
  });
});

describe("steered marker", () => {
  it("marks a message that was sent into the running turn", () => {
    const { container } = render(
      <MessageBubble
        message={{ role: "user", type: "text", content: "not that file", steered: true }}
        sessionId="s1"
      />
    );
    expect(container.querySelector(".msg-steered-marker")?.textContent).toContain(
      "sent to the running turn"
    );
  });

  it("leaves an ordinary user message unmarked", () => {
    // A queued message and a steered one look identical in the transcript but
    // behave very differently; only the steered one says so.
    const { container } = render(
      <MessageBubble
        message={{ role: "user", type: "text", content: "do this next" }}
        sessionId="s1"
      />
    );
    expect(container.querySelector(".msg-steered-marker")).toBeNull();
  });
});
