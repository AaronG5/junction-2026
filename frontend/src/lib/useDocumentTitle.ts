import { useEffect } from "react";

/** Sets the browser tab title: "Claims · Providence", or just "Providence". */
export function useDocumentTitle(title?: string) {
  useEffect(() => {
    document.title = title ? `${title} · Providence` : "Providence";
  }, [title]);
}
