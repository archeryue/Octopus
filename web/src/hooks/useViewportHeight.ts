import { useEffect } from "react";

// Drives --app-h / --app-top from the Visual Viewport. On iOS the layout
// viewport doesn't shrink for the keyboard or the form-assistant bar
// (shown with external keyboards), so position:fixed roots leave gaps.
export function useViewportHeight() {
  useEffect(() => {
    const root = document.documentElement;
    const vv = window.visualViewport;

    function sync() {
      const h = vv ? vv.height : window.innerHeight;
      const offsetTop = vv ? vv.offsetTop : 0;
      root.style.setProperty("--app-h", `${h}px`);
      root.style.setProperty("--app-top", `${offsetTop}px`);
    }

    sync();
    vv?.addEventListener("resize", sync);
    vv?.addEventListener("scroll", sync);
    window.addEventListener("orientationchange", sync);

    return () => {
      vv?.removeEventListener("resize", sync);
      vv?.removeEventListener("scroll", sync);
      window.removeEventListener("orientationchange", sync);
    };
  }, []);
}
