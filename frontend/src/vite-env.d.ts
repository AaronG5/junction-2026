/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** "true" only in demo mode (vite --mode demo). */
  readonly VITE_DEMO_DATA: string;
}
interface ImportMeta {
  readonly env: ImportMetaEnv;
}
