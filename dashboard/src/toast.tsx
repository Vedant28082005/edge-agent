import { createContext, useCallback, useContext, useRef, useState } from "react";
import type { ReactNode } from "react";

type Toast = { id: number; text: string; kind: "error" | "ok" };
const Ctx = createContext<(text: string, kind?: Toast["kind"]) => void>(() => { /* no provider (tests render views alone) */ });
export const useToast = () => useContext(Ctx);

/** One-line toasts, 4 s, announced politely. Errors are also kept inline where they happened. */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const n = useRef(0);
  const push = useCallback((text: string, kind: Toast["kind"] = "error") => {
    const id = ++n.current;
    setItems((l) => [...l.filter((t) => t.text !== text), { id, text, kind }].slice(-3));
    setTimeout(() => setItems((l) => l.filter((t) => t.id !== id)), 4000);
  }, []);
  return (
    <Ctx.Provider value={push}>
      {children}
      <div className="toasts" aria-live="polite">
        {items.map((t) => <div key={t.id} className={`toast toast-${t.kind}`}>{t.text}</div>)}
      </div>
    </Ctx.Provider>
  );
}
