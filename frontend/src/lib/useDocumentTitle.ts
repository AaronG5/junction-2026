import { useEffect } from "react";

/** Sets the browser tab title: "Claims · Veritas Lens", or just "Veritas Lens". */
export function useDocumentTitle(title?: string) {
  useEffect(() => {
    document.title = title ? `${title} · Veritas Lens` : "Veritas Lens";
  }, [title]);
}
