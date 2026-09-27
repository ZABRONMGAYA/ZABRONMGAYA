import { CircleCheck, CircleX, Info, X } from "lucide-react";

import { usePick } from "../state/store";

const ICON = { success: CircleCheck, error: CircleX, info: Info } as const;

export function Toasts() {
  const { toasts, dismiss } = usePick("toasts", "dismiss");
  return (
    <div className="sy-toasts" role="status" aria-live="polite">
      {toasts.map((t) => {
        const Icon = ICON[t.kind];
        return (
          <div key={t.id} className="sy-toast" data-testid={`toast-${t.kind}`}>
            <Icon size={20} className={`sy-toast__icon--${t.kind}`} aria-hidden />
            <span className="sy-toast__text">{t.text}</span>
            <button type="button" className="sy-toast__close" aria-label="Dismiss" onClick={() => dismiss(t.id)}>
              <X size={14} aria-hidden />
            </button>
          </div>
        );
      })}
    </div>
  );
}
