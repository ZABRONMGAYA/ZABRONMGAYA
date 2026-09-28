// Syncora design-system components (SYNCORA_COMPONENT_MAP.md). Screens compose these; styles live in base.css.
import {
  Anchor,
  Check,
  ChevronDown,
  CircleX,
  LoaderCircle,
  Lock,
  type LucideIcon,
  Minus,
  TriangleAlert,
  X,
} from "lucide-react";
import { type ButtonHTMLAttributes, type ReactNode, useEffect, useId, useRef } from "react";

import symbolDark from "./assets/syncora-symbol-dark.svg";
import symbolLight from "./assets/syncora-symbol.svg";
import symbolPaper from "./assets/syncora-symbol-paper-solid.svg";

export type ButtonVariant =
  | "primary"
  | "secondary"
  | "tertiary"
  | "ghost"
  | "icon"
  | "destructive"
  | "success"
  | "warning";
export type ButtonSize = "compact" | "default" | "36" | "dialog" | "hero";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  icon?: LucideIcon;
  iconSize?: number;
  shortcut?: string;
  loading?: boolean;
  loadingLabel?: string;
}

export function Button({
  variant = "secondary",
  size = "default",
  icon: Icon,
  iconSize = 14,
  shortcut,
  loading = false,
  loadingLabel = "Working…",
  children,
  className = "",
  disabled,
  ...rest
}: ButtonProps) {
  const sizeClass = size === "default" ? "" : `sy-btn--${size}`;
  return (
    <button
      type="button"
      className={`sy-btn sy-btn--${variant} ${sizeClass} ${className}`}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading ? (
        <LoaderCircle size={iconSize} className="sy-spin" aria-hidden />
      ) : (
        Icon && <Icon size={iconSize} aria-hidden />
      )}
      {loading ? loadingLabel : children}
      {shortcut && !loading && <span className="sy-shortcut">{shortcut}</span>}
    </button>
  );
}

/**
 * The brand mark: paper bars and red line on dark surfaces, ink bars on light ones (the theme picks, in base.css);
 * all paper as the "Sync" action icon.
 */
export function SyncoraSymbol({ size = 20, variant = "dark" }: { size?: number; variant?: "dark" | "paper" }) {
  const img = (src: string, className?: string) => (
    <img
      src={src}
      width={size}
      height={size}
      alt=""
      aria-hidden
      draggable={false}
      className={className}
      style={{ display: "block", flex: "none" }}
    />
  );
  if (variant === "paper") return img(symbolPaper);
  return (
    <>
      {img(symbolDark, "sy-symbol--on-dark")}
      {img(symbolLight, "sy-symbol--on-light")}
    </>
  );
}

export function Eyebrow({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`sy-eyebrow ${className}`}>{children}</div>;
}

export function KeyHint({ keys }: { keys: string }) {
  return <span className="sy-keyhint">{keys}</span>;
}

/** Platform shortcut text: ⌘ ⇧ ⌥ on macOS, Ctrl Shift Alt elsewhere. */
export function shortcut(mac: string): string {
  if (typeof navigator !== "undefined" && /Mac/.test(navigator.platform)) return mac;
  return mac.replace(/⌘/g, "Ctrl+").replace(/⇧/g, "Shift+").replace(/⌥/g, "Alt+");
}

export type SyncStatus =
  | "reference"
  | "high"
  | "good"
  | "review"
  | "failed"
  | "running"
  | "none"
  | "locked"
  | "skipped";

const BADGE: Record<SyncStatus, { label: string; icon: LucideIcon }> = {
  reference: { label: "Reference", icon: Anchor },
  high: { label: "High", icon: Check },
  good: { label: "Good", icon: Check },
  review: { label: "Review", icon: TriangleAlert },
  failed: { label: "Failed", icon: X },
  running: { label: "Analyzing", icon: LoaderCircle },
  none: { label: "Not synced", icon: Minus },
  locked: { label: "Locked", icon: Lock },
  skipped: { label: "Skipped", icon: Minus },
};

/** Status is always icon + word (+ number): colour is never the only cue. */
export function SyncBadge({
  status,
  confidence,
  label,
  size = "sm",
}: {
  status: SyncStatus;
  confidence?: number | null;
  label?: string;
  size?: "sm" | "md";
}) {
  const { label: word, icon: Icon } = BADGE[status];
  const pct = confidence !== undefined && confidence !== null ? ` · ${Math.round(confidence * 100)}%` : "";
  return (
    <span className={`sy-badge sy-badge--${status} ${size === "md" ? "sy-badge--md" : ""}`}>
      <Icon size={size === "md" ? 13 : 12} aria-hidden className={status === "running" ? "sy-spin" : undefined} />
      {(label ?? word) + pct}
    </span>
  );
}

export function StatusSquare({ status }: { status: "ok" | "review" | "failed" | "none" }) {
  const Icon = status === "ok" ? Check : status === "review" ? TriangleAlert : status === "failed" ? X : Minus;
  const word = { ok: "Synchronized", review: "Review", failed: "Failed", none: "Not synced" }[status];
  return (
    <span className={`sy-status-square sy-status-square--${status}`} title={word} aria-label={word} role="img">
      <Icon size={12} aria-hidden />
    </span>
  );
}

export function ConfidenceBar({
  value,
  tone,
  height = 4,
}: {
  value: number;
  tone: "high" | "good" | "review" | "running";
  height?: 2 | 4 | 8;
}) {
  return (
    <div className={`sy-bar sy-bar--${tone}`} style={{ height }} role="presentation">
      <span style={{ width: `${Math.max(0, Math.min(1, value)) * 100}%` }} />
    </div>
  );
}

/** 20 cells: paper when the stage is complete, red while it runs, empty cells elevated. */
export function SegmentedProgress({ value, complete }: { value: number; complete: boolean }) {
  const on = Math.round(Math.max(0, Math.min(1, value)) * 20);
  return (
    <div
      className={`sy-segments ${complete ? "sy-segments--complete" : ""}`}
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(value * 100)}
    >
      {Array.from({ length: 20 }, (_, k) => (
        <span key={k} className={k < on ? "on" : undefined} />
      ))}
    </div>
  );
}

export function Toggle({
  on,
  onChange,
  label,
  disabled,
}: {
  on: boolean;
  onChange: (on: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      className="sy-toggle"
      disabled={disabled}
      onClick={() => onChange(!on)}
    />
  );
}

export function Segmented<T extends string>({
  options,
  value,
  onChange,
  label,
  variant,
}: {
  options: { value: T; label: ReactNode; title?: string }[];
  value: T;
  onChange: (value: T) => void;
  label: string;
  variant?: "method";
}) {
  return (
    <div className={`sy-segmented ${variant ? `sy-segmented--${variant}` : ""}`} role="group" aria-label={label}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          aria-pressed={o.value === value}
          title={o.title}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Select<T extends string | number>({
  value,
  options,
  onChange,
  label,
  width,
}: {
  value: T;
  options: { value: T; label: string }[];
  onChange: (value: T) => void;
  label: string;
  width?: number;
}) {
  return (
    <span className="sy-select-wrap" style={width ? { width } : undefined}>
      <select
        className="sy-select"
        aria-label={label}
        value={String(value)}
        onChange={(e) => {
          const hit = options.find((o) => String(o.value) === e.target.value);
          if (hit) onChange(hit.value);
        }}
      >
        {options.map((o) => (
          <option key={String(o.value)} value={String(o.value)}>
            {o.label}
          </option>
        ))}
      </select>
      <ChevronDown size={14} aria-hidden />
    </span>
  );
}

export function SettingRow({
  title,
  description,
  children,
}: {
  title: string;
  description?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="sy-setting-row">
      <div>
        <div className="sy-setting-row__title">{title}</div>
        {description && <div className="sy-setting-row__desc">{description}</div>}
      </div>
      <div className="sy-setting-row__control">{children}</div>
    </div>
  );
}

export function EmptyState({
  icon: Icon,
  title,
  body,
  action,
}: {
  icon: LucideIcon;
  title: string;
  body: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="sy-empty">
      <Icon size={28} className="sy-empty__icon" aria-hidden />
      <div className="sy-empty__title">{title}</div>
      <div className="sy-empty__body">{body}</div>
      {action && <div>{action}</div>}
    </div>
  );
}

export function Dialog({
  title,
  icon: Icon = CircleX,
  iconTone = "error",
  children,
  footer,
  onClose,
  wide,
  testId,
}: {
  title: string;
  icon?: LucideIcon;
  iconTone?: "error" | "warning" | "info";
  children: ReactNode;
  footer: ReactNode;
  onClose: () => void;
  wide?: boolean;
  testId?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.querySelector<HTMLElement>("button, [href], input, select, textarea")?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      previous?.focus?.();
    };
  }, [onClose]);
  const tone =
    iconTone === "error"
      ? "var(--sy-status-error)"
      : iconTone === "warning"
        ? "var(--sy-status-warning)"
        : "var(--sy-text-2)";
  return (
    <>
      <div className="sy-backdrop" onClick={onClose} />
      <div
        ref={ref}
        className={`sy-dialog ${wide ? "sy-dialog--wide" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        data-testid={testId}
      >
        <div className="sy-dialog__head">
          <Icon size={20} style={{ color: tone }} aria-hidden />
          <h2 className="sy-dialog__title" id={titleId}>
            {title}
          </h2>
        </div>
        <div className="sy-dialog__body">{children}</div>
        <div className="sy-dialog__foot">{footer}</div>
      </div>
    </>
  );
}

/** Confidence → badge status, with the project's review threshold (default 85%). */
export function statusFor(confidence: number | null, threshold: number): "high" | "good" | "review" {
  if (confidence === null) return "review";
  if (confidence >= 0.95) return "high";
  if (confidence >= threshold) return "good";
  return "review";
}
