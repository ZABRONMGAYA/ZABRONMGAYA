// Keeps an error in one view from blanking the whole window: the view is replaced by a message with a way back.
import { TriangleAlert } from "lucide-react";
import { Component, type ErrorInfo, type ReactNode } from "react";

import { Button, EmptyState } from "../design-system/components";

interface Props {
  children: ReactNode;
  /** What the view is called in the message ("the timeline"). */
  what: string;
  /** The view is a dialog: the message covers the window, and Close closes the dialog. */
  onClose?: () => void;
}

export class ErrorBoundary extends Component<Props, { error: Error | null }> {
  override state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`Syncora: ${this.props.what} stopped`, error, info.componentStack);
  }

  override render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div
        className={`sy-error-view ${this.props.onClose ? "sy-error-view--overlay" : ""}`}
        role="alert"
        data-testid="view-error"
      >
        <EmptyState
          icon={TriangleAlert}
          title={`Could not show ${this.props.what}`}
          body={
            <>
              Your project is safe: nothing was lost. <span className="sy-error-view__detail">{error.message}</span>
            </>
          }
          action={
            <div className="sy-error-view__actions">
              <Button
                variant="primary"
                size="compact"
                onClick={() => {
                  this.props.onClose?.();
                  this.setState({ error: null });
                }}
              >
                {this.props.onClose ? "Close" : "Try again"}
              </Button>
              <Button variant="secondary" size="compact" onClick={() => window.location.reload()}>
                Reload window
              </Button>
            </div>
          }
        />
      </div>
    );
  }
}
