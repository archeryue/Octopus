/**
 * Renderer-dispatch tests for FileViewerDialog.
 *
 * The dialog hits two endpoints — /meta returns the `kind` (which
 * picks the renderer), then /files returns the bytes. We mock
 * window.fetch so each test can stub a specific kind without
 * needing a real backend. The relationship between extension and
 * kind is enforced server-side and covered by tests/test_file_viewer.py;
 * here we only verify the frontend acts on kind correctly.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";

import { FileViewerDialog } from "./FileViewerDialog";
import { useSessionStore } from "../stores/sessionStore";

interface MetaPayload {
  kind: "markdown" | "code" | "text" | "image" | "pdf";
  mime_type: string;
  size: number;
}

function mockFetch(meta: MetaPayload, body: string) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    // Keep the requested path in the response so CodeBody can infer
    // the language hint from the extension (e.g. main.py → python).
    const match = url.match(/[?&]path=([^&]+)/);
    const reqPath = match ? decodeURIComponent(match[1]) : "test";
    if (url.includes("/files/meta")) {
      return new Response(JSON.stringify({ path: reqPath, ...meta }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    return new Response(body, {
      status: 200,
      headers: { "content-type": meta.mime_type },
    });
  });
}

beforeEach(() => {
  useSessionStore.setState({ token: "tok", viewer: null });
});

afterEach(() => {
  cleanup();
  useSessionStore.setState({ viewer: null });
  vi.restoreAllMocks();
});

describe("FileViewerDialog", () => {
  it("renders nothing while viewer state is null", () => {
    render(<FileViewerDialog />);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("renders markdown body for kind=markdown", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch(
        { kind: "markdown", mime_type: "text/markdown; charset=utf-8", size: 22 },
        "# Hello\n\nWorld."
      )
    );
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "doc.md"));

    // The H1 the markdown renderer produces
    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Hello" })).toBeInTheDocument()
    );
  });

  it("renders <img> for kind=image", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch(
        { kind: "image", mime_type: "image/png", size: 1024 },
        "ignored-binary"
      )
    );
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "shot.png"));

    await waitFor(() => {
      const img = document.querySelector("img");
      expect(img).not.toBeNull();
      expect(img!.getAttribute("src")).toContain("/files?path=shot.png");
      expect(img!.getAttribute("src")).toContain("token=tok");
    });
  });

  it("renders sandboxed <iframe> for kind=pdf", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch(
        { kind: "pdf", mime_type: "application/pdf", size: 8000 },
        "ignored"
      )
    );
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "doc.pdf"));

    await waitFor(() => {
      const iframe = document.querySelector("iframe");
      expect(iframe).not.toBeNull();
      expect(iframe!.getAttribute("src")).toContain("/files?path=doc.pdf");
      expect(iframe!.getAttribute("sandbox")).toContain("allow-same-origin");
    });
  });

  it("renders <pre><code> for kind=code", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch(
        { kind: "code", mime_type: "text/plain; charset=utf-8", size: 14 },
        "def main():\n    pass\n"
      )
    );
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "main.py"));

    await waitFor(() => {
      const code = document.querySelector("pre code");
      expect(code).not.toBeNull();
      expect(code!.textContent).toContain("def main");
      expect(code!.className).toContain("language-python");
    });
  });

  it("renders plain <pre> for kind=text", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch(
        { kind: "text", mime_type: "text/plain; charset=utf-8", size: 5 },
        "logged"
      )
    );
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "out.log"));

    await waitFor(() => {
      // text body is a bare <pre> with text directly inside (no <code>)
      const pres = document.querySelectorAll("pre");
      const match = Array.from(pres).find(
        (p) => p.querySelector("code") === null && p.textContent?.includes("logged")
      );
      expect(match).toBeDefined();
    });
  });

  it("shows an error when the meta endpoint returns 404", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ detail: "File not found: nope.md" }), {
          status: 404,
          headers: { "content-type": "application/json" },
        })
      )
    );
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "nope.md"));

    await waitFor(() =>
      expect(screen.getByText(/Couldn't open file/i)).toBeInTheDocument()
    );
    expect(screen.getByText(/File not found/i)).toBeInTheDocument();
  });

  it("closes when the close button is clicked", async () => {
    vi.stubGlobal(
      "fetch",
      mockFetch(
        { kind: "text", mime_type: "text/plain; charset=utf-8", size: 1 },
        "x"
      )
    );
    render(<FileViewerDialog />);
    act(() => useSessionStore.getState().openViewer("s1", "x.txt"));

    await waitFor(() => expect(document.querySelector("pre")).not.toBeNull());
    const closeBtn = screen.getByRole("button", { name: /close/i });
    act(() => closeBtn.click());
    expect(useSessionStore.getState().viewer).toBeNull();
  });
});
