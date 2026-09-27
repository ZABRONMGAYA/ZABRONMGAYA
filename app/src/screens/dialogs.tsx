// Project dialogs: resume previous work, duplicates, assign camera / group, remove from project.
import { CopyCheck, History, Trash2, Video } from "lucide-react";
import { useEffect, useState } from "react";

import { call } from "../api/client";
import type { Duplicate } from "../api/contract";
import { Button, Dialog, Select } from "../design-system/components";
import { useProd } from "../state/production";
import { useApp } from "../state/store";
import { sessionLabel } from "./media/MediaScreen";

export function Dialogs() {
  const dialog = useProd((s) => s.dialog);
  const close = () => useProd.getState().openDialog(null);
  switch (dialog) {
    case "resume":
      return <ResumeDialog onClose={close} />;
    case "duplicates":
      return <DuplicatesDialog onClose={close} />;
    case "assign-camera":
      return <AssignCameraDialog onClose={close} />;
    case "assign-session":
      return <AssignSessionDialog onClose={close} />;
    case "remove":
      return <RemoveDialog onClose={close} />;
    default:
      return null;
  }
}

const KIND_WORD: Record<string, string> = {
  probe: "files to read",
  analyze: "clips to analyse",
  match: "pairs to verify",
  extend: "extended searches",
};

function ResumeDialog({ onClose }: { onClose: () => void }) {
  const resume = useProd((s) => s.resume);
  if (!resume) return null;
  const parts = Object.entries(resume.pending).map(([k, n]) => `${n.toLocaleString()} ${KIND_WORD[k] ?? k}`);
  return (
    <Dialog
      title="Previous analysis found"
      icon={History}
      iconTone="info"
      onClose={onClose}
      testId="resume-dialog"
      footer={
        <>
          <Button variant="ghost" size="36" onClick={onClose}>
            Later
          </Button>
          <span className="sy-spacer" />
          <Button
            variant="secondary"
            size="36"
            onClick={() => void useProd.getState().restart()}
            data-testid="restart-work"
          >
            Restart
          </Button>
          <Button
            variant="primary"
            size="36"
            onClick={() => void useProd.getState().resumePipeline()}
            data-testid="resume-work"
          >
            Resume
          </Button>
        </>
      }
    >
      <p>
        This project was closed with work still to do{parts.length ? `: ${parts.join(", ")}` : ""}
        {resume.sync ? `, and a sync that had reached “${resume.sync.phase}”` : ""}. Everything finished before is kept.
      </p>
      <p>
        Resume continues where it stopped. Restart queues the unfinished work again from the beginning (analysis already
        cached is reused).
      </p>
    </Dialog>
  );
}

function DuplicatesDialog({ onClose }: { onClose: () => void }) {
  const [rows, setRows] = useState<Duplicate[] | null>(null);
  useEffect(() => void call("media.duplicates", {}).then(setRows, () => setRows([])), []);
  const decide = (ids: number[], decision: "keep" | "ignore") =>
    void useApp.getState().run(async () => {
      const result = await call("media.decide_duplicates", { media_ids: ids, decision });
      setRows(result.duplicates);
      await useProd.getState().loadIndex();
    });
  return (
    <Dialog
      title="Possible duplicates"
      icon={CopyCheck}
      iconTone="info"
      wide
      onClose={onClose}
      testId="duplicates-dialog"
      footer={
        <>
          <span className="sy-muted">Nothing is deleted. Ignored copies stay in the project, left out of sync.</span>
          <span className="sy-spacer" />
          <Button
            variant="secondary"
            size="36"
            disabled={!rows?.length}
            onClick={() =>
              rows &&
              decide(
                rows.map((r) => r.media_id),
                "keep",
              )
            }
          >
            Keep all
          </Button>
          <Button
            variant="secondary"
            size="36"
            disabled={!rows?.length}
            onClick={() =>
              rows &&
              decide(
                rows.map((r) => r.media_id),
                "ignore",
              )
            }
          >
            Ignore all duplicates
          </Button>
          <Button variant="primary" size="36" onClick={onClose}>
            Done
          </Button>
        </>
      }
    >
      {rows === null ? (
        <p>Loading…</p>
      ) : rows.length === 0 ? (
        <p>No duplicates.</p>
      ) : (
        <div className="sy-dups">
          {rows.map((d) => (
            <div key={d.media_id} className="sy-dups__row">
              <div className="sy-dups__files">
                <div className="sy-ellipsis" title={d.path}>
                  <strong>{d.filename}</strong> <span className="sy-mono sy-muted">{d.path}</span>
                </div>
                <div className="sy-ellipsis sy-muted" title={d.original_path}>
                  {d.reason === "identical" ? "Identical to" : "Probably a copy of"} {d.original_filename}{" "}
                  <span className="sy-mono">{d.original_path}</span>
                </div>
              </div>
              <span className="sy-dups__state">
                {d.decision === "keep" ? "Kept" : d.decision === "ignore" ? "Ignored" : "Undecided"}
              </span>
              <Button
                variant={d.decision === "keep" ? "tertiary" : "secondary"}
                size="compact"
                onClick={() => decide([d.media_id], "keep")}
              >
                Keep both
              </Button>
              <Button
                variant={d.decision === "ignore" ? "tertiary" : "secondary"}
                size="compact"
                onClick={() => decide([d.media_id], "ignore")}
              >
                Ignore duplicate
              </Button>
            </div>
          ))}
        </div>
      )}
    </Dialog>
  );
}

function AssignCameraDialog({ onClose }: { onClose: () => void }) {
  const devices = useProd((s) => s.devices);
  const ids = [...useProd((s) => s.selection)];
  const [target, setTarget] = useState<number | "new">(devices[0]?.id ?? "new");
  const [name, setName] = useState("Camera");
  return (
    <Dialog
      title={`Assign ${ids.length.toLocaleString()} clip${ids.length === 1 ? "" : "s"} to a camera`}
      icon={Video}
      iconTone="info"
      onClose={onClose}
      footer={
        <>
          <span className="sy-spacer" />
          <Button variant="secondary" size="36" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            size="36"
            onClick={() => void useProd.getState().assignCamera(ids, target === "new" ? { name } : target)}
          >
            Assign
          </Button>
        </>
      }
    >
      <p>
        Clips from one camera never overlap in time, so this also tells the sync which clips cannot be matched together.
      </p>
      <div className="sy-field">
        <Select
          label="Camera"
          value={target}
          onChange={setTarget}
          options={[
            ...devices.map((d) => ({ value: d.id, label: `${d.name} (${d.clips})` })),
            { value: "new" as const, label: "New camera…" },
          ]}
        />
        {target === "new" && (
          <input className="sy-input" value={name} onChange={(e) => setName(e.target.value)} aria-label="Camera name" />
        )}
      </div>
    </Dialog>
  );
}

function AssignSessionDialog({ onClose }: { onClose: () => void }) {
  const sessions = useProd((s) => s.sessions);
  const ids = [...useProd((s) => s.selection)];
  const [target, setTarget] = useState<number | "new" | "none">(sessions[0]?.id ?? "new");
  const [label, setLabel] = useState("Session");
  return (
    <Dialog
      title={`Move ${ids.length.toLocaleString()} clip${ids.length === 1 ? "" : "s"} to a group`}
      icon={Video}
      iconTone="info"
      onClose={onClose}
      footer={
        <>
          <span className="sy-spacer" />
          <Button variant="secondary" size="36" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            size="36"
            onClick={() =>
              void useProd
                .getState()
                .assignSession(ids, target === "new" ? { label } : target === "none" ? null : target)
            }
          >
            Move
          </Button>
        </>
      }
    >
      <p>
        Groups organise the production (ceremony, speeches, day 2…). A group you create by hand is kept when sessions
        are recalculated.
      </p>
      <div className="sy-field">
        <Select
          label="Group"
          value={target}
          onChange={setTarget}
          options={[
            ...sessions.map((s) => ({ value: s.id, label: `${sessionLabel(s)} (${s.clips})` })),
            { value: "new" as const, label: "New group…" },
            { value: "none" as const, label: "Unmatched (no group)" },
          ]}
        />
        {target === "new" && (
          <input
            className="sy-input"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            aria-label="Group name"
          />
        )}
      </div>
    </Dialog>
  );
}

function RemoveDialog({ onClose }: { onClose: () => void }) {
  const ids = [...useProd((s) => s.selection)];
  return (
    <Dialog
      title={`Remove ${ids.length.toLocaleString()} clip${ids.length === 1 ? "" : "s"} from the project?`}
      icon={Trash2}
      iconTone="warning"
      onClose={onClose}
      testId="remove-dialog"
      footer={
        <>
          <Button variant="ghost" size="36" onClick={onClose}>
            Cancel
          </Button>
          <span className="sy-spacer" />
          <Button
            variant="destructive"
            size="36"
            onClick={() => void useProd.getState().remove(ids)}
            data-testid="confirm-remove"
          >
            Remove from project
          </Button>
        </>
      }
    >
      <p>
        The clips, their matches and their queued work leave this project.{" "}
        <strong>The files on disk are not touched</strong>: import them again at any time.
      </p>
    </Dialog>
  );
}
