import { usePick } from "../state/store";

export function Toasts() {
  const { toasts, dismiss } = usePick("toasts", "dismiss");
  return (
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`toast ${t.kind}`} data-testid={`toast-${t.kind}`}>
          <span>{t.text}</span>
          <button className="icon" aria-label="Dismiss" onClick={() => dismiss(t.id)}>
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
