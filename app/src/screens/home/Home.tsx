// S01 Home: start or resume work. Sidebar (brand, navigation, settings), hero row, recent projects.
import {
  BookOpen,
  Check,
  Clapperboard,
  FolderOpen,
  House,
  Import,
  LayoutGrid,
  Plus,
  Settings,
  TriangleAlert,
} from "lucide-react";
import { useState } from "react";

import { Button, KeyHint, SyncoraSymbol, shortcut } from "../../design-system/components";
import { PRESETS, defaultPresetId, setDefaultPreset } from "../../lib/presets";
import { useProd } from "../../state/production";
import { usePick } from "../../state/store";

function fileName(path: string): string {
  return (path.split(/[\\/]/).pop() ?? path).replace(/\.(syncora|mcsync)$/, "");
}

type Page = "home" | "presets" | "learn";

export function Home() {
  const { engine, recent, newProject, openProject } = usePick("engine", "recent", "newProject", "openProject");
  const [page, setPage] = useState<Page>("home");
  const ready = engine.state === "ready";
  const ffmpegMissing = ready && !engine.hello?.ffmpeg;

  return (
    <div className="sy-home" data-testid="welcome">
      <aside className="sy-home__side">
        <div className="sy-home__brand">
          <SyncoraSymbol size={22} />
          <span>Syncora</span>
        </div>
        {/* DESIGN-OPEN: #10 Projects/Presets/Learn pages are not drawn: Projects opens a project; Presets and Learn
            follow the Home layout (DESIGN_DECISIONS.md D-03). */}
        <nav className="sy-home__nav" aria-label="Home">
          <button
            type="button"
            className="sy-navitem"
            aria-current={page === "home" ? "page" : undefined}
            onClick={() => setPage("home")}
          >
            <House size={16} aria-hidden /> Home
          </button>
          <button type="button" className="sy-navitem" onClick={() => void openProject()} disabled={!ready}>
            <Clapperboard size={16} aria-hidden /> Projects
          </button>
          <button
            type="button"
            className="sy-navitem"
            aria-current={page === "presets" ? "page" : undefined}
            onClick={() => setPage("presets")}
            data-testid="home-presets"
          >
            <LayoutGrid size={16} aria-hidden /> Presets
          </button>
          <button
            type="button"
            className="sy-navitem"
            aria-current={page === "learn" ? "page" : undefined}
            onClick={() => setPage("learn")}
            data-testid="home-learn"
          >
            <BookOpen size={16} aria-hidden /> Learn
          </button>
        </nav>
        <div className="sy-home__bottom">
          <button
            type="button"
            className="sy-navitem sy-navitem--muted"
            onClick={() => useProd.getState().openSettings("performance")}
          >
            <Settings size={16} aria-hidden /> Settings
          </button>
          <div className="sy-home__license">
            <div>Syncora</div>
            <div>v{engine.hello?.version ?? "…"}</div>
          </div>
        </div>
      </aside>
      {page === "presets" ? (
        <PresetsPage ready={ready} onCreate={(id) => void newProject(id)} />
      ) : page === "learn" ? (
        <LearnPage ready={ready} onCreate={() => void newProject()} />
      ) : (
        <main className="sy-home__main">
          <header className="sy-home__header">
            <h1>Home</h1>
          </header>
          <div className="sy-home__content">
            <div className="sy-hero">
              <button
                type="button"
                className="sy-hero__new"
                onClick={() => void newProject()}
                disabled={!ready}
                data-testid="new-project"
              >
                <span className="sy-hero__top">
                  <Plus size={28} aria-hidden />
                  <span className="sy-hero__key">{shortcut("⌘N")}</span>
                </span>
                <span>
                  <span className="sy-hero__title">New project</span>
                  <span className="sy-hero__sub">
                    Create a project and drop your camera cards. Syncora does the rest.
                  </span>
                </span>
              </button>
              <button
                type="button"
                className="sy-hero__tile"
                onClick={() => void openProject()}
                disabled={!ready}
                data-testid="open-project"
              >
                <span className="sy-hero__top">
                  <FolderOpen size={24} aria-hidden />
                  <span className="sy-hero__key sy-muted">{shortcut("⌘O")}</span>
                </span>
                <span>
                  <span className="sy-hero__tile-title">Open project</span>
                  <span className="sy-hero__tile-sub">.syncora file from disk</span>
                </span>
              </button>
              <button type="button" className="sy-hero__tile" onClick={() => void newProject()} disabled={!ready}>
                <span className="sy-hero__top">
                  <Import size={24} aria-hidden />
                  <span className="sy-hero__key sy-muted">{shortcut("⌘I")}</span>
                </span>
                <span>
                  <span className="sy-hero__tile-title">Quick sync from cards</span>
                  <span className="sy-hero__tile-sub">New project, then drop folders or cards</span>
                </span>
              </button>
            </div>

            {engine.state === "starting" && <p className="sy-muted">Starting the sync engine…</p>}
            {engine.state === "crashed" && (
              <div className="sy-banner" role="alert">
                <TriangleAlert size={20} className="sy-banner__icon" aria-hidden />
                <div>
                  <div className="sy-banner__title">The sync engine could not start.</div>
                  <pre className="sy-banner__body sy-mono">{engine.error}</pre>
                </div>
              </div>
            )}
            {ffmpegMissing && (
              <div className="sy-banner" role="alert">
                <TriangleAlert size={20} className="sy-banner__icon" aria-hidden />
                <div>
                  <div className="sy-banner__title">FFmpeg was not found</div>
                  <div className="sy-banner__body">
                    Media cannot be read. Reinstall Syncora, or install FFmpeg and restart.
                  </div>
                </div>
              </div>
            )}

            <section>
              <div className="sy-recent__head">
                <span className="sy-eyebrow">Recent projects</span>
                <span className="sy-muted">Last opened first</span>
              </div>
              {recent.length === 0 ? (
                <div className="sy-empty">
                  <Clapperboard size={28} className="sy-empty__icon" aria-hidden />
                  <div className="sy-empty__title">No projects yet</div>
                  <div className="sy-empty__body">
                    Create a project and drop your camera cards. Syncora groups clips by camera automatically.
                  </div>
                </div>
              ) : (
                <div className="sy-recent__grid">
                  {recent.map((path) => (
                    <button
                      key={path}
                      type="button"
                      className="sy-project-card"
                      onClick={() => void openProject(path)}
                      disabled={!ready}
                      title={path}
                      aria-label={fileName(path)}
                    >
                      <span className="sy-project-card__thumbs" aria-hidden>
                        <span />
                        <span />
                        <span />
                        <span />
                      </span>
                      <span className="sy-project-card__body">
                        <span className="sy-project-card__name">{fileName(path)}</span>
                        <span className="sy-project-card__path sy-mono">{path}</span>
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </section>
            <div className="sy-home__keys sy-muted">
              <KeyHint keys={shortcut("⌘N")} /> new project · <KeyHint keys={shortcut("⌘O")} /> open project
            </div>
          </div>
        </main>
      )}
    </div>
  );
}

function PresetsPage({ ready, onCreate }: { ready: boolean; onCreate: (id: string) => void }) {
  const [chosen, setChosen] = useState(defaultPresetId);
  return (
    <main className="sy-home__main" data-testid="presets-page">
      <header className="sy-home__header">
        <h1>Presets</h1>
      </header>
      <div className="sy-home__content">
        <p className="sy-dim sy-home__lead">
          Starting settings for a kind of shoot. The default preset is used for every new project; each project can
          still change them in Settings → Synchronization.
        </p>
        <div className="sy-presets">
          {PRESETS.map((p) => (
            <article
              key={p.id}
              className={`sy-preset ${chosen === p.id ? "sy-preset--default" : ""}`}
              data-testid={`preset-${p.id}`}
            >
              <div className="sy-preset__head">
                <h2>{p.name}</h2>
                {chosen === p.id && (
                  <span className="sy-preset__chip">
                    <Check size={12} aria-hidden /> Default
                  </span>
                )}
              </div>
              <p>{p.description}</p>
              <ul>
                {p.summary.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
              <div className="sy-preset__actions">
                <Button variant="primary" size="compact" disabled={!ready} onClick={() => onCreate(p.id)}>
                  New project
                </Button>
                <Button
                  variant="ghost"
                  size="compact"
                  disabled={chosen === p.id}
                  onClick={() => {
                    setDefaultPreset(p.id);
                    setChosen(p.id);
                  }}
                >
                  Make default
                </Button>
              </div>
            </article>
          ))}
        </div>
      </div>
    </main>
  );
}

const STEPS: { title: string; body: string }[] = [
  {
    title: "Create a project",
    body: "Choose where the .syncora file goes, or start from a preset for your kind of shoot. Syncora never modifies, moves or deletes your media.",
  },
  {
    title: "Import media",
    body: "Drop camera cards, folders or files anywhere on the window. Clips are grouped by camera automatically; metadata and audio are read in the background while you work.",
  },
  {
    title: "Synchronize",
    body: "Sync all (⇧S) matches every recording by its sound, guided by timecode and recording times. Anything uncertain is marked REVIEW and never locked automatically.",
  },
  {
    title: "Analyze with AI",
    body: "Transcribe the project on this computer, name the speakers, and search what was said (⌘K). Clips audio could not place can be found from speech and light changes (⇧A).",
  },
  {
    title: "Check the timeline",
    body: "Select a clip to see why it was placed where it is; drag, nudge with the arrow keys or snap it to the audio. Undo takes any correction back.",
  },
  {
    title: "Export",
    body: "Export XML (⌘E) writes a ready timeline for DaVinci Resolve, Premiere Pro or Final Cut Pro, linked to your original files.",
  },
];

const TIPS = [
  "Start the audio recorder first and let it run: every camera clip then overlaps one long reference.",
  "A clap or a slate at the start of a take gives a sharp moment every microphone hears.",
  "Leave the cameras' own microphones on, even when the sound comes from a recorder.",
  "Set the camera clocks, or jam-sync timecode: clips are found faster in large productions.",
  "Sessions keep separate days or events apart; assign clips to a session when the clocks are wrong.",
];

function LearnPage({ ready, onCreate }: { ready: boolean; onCreate: () => void }) {
  return (
    <main className="sy-home__main" data-testid="learn-page">
      <header className="sy-home__header">
        <h1>Learn</h1>
      </header>
      <div className="sy-home__content">
        <p className="sy-dim sy-home__lead">From camera cards to an editable multicam timeline, in six steps.</p>
        <ol className="sy-learn">
          {STEPS.map((step, k) => (
            <li key={step.title}>
              <span className="sy-learn__n">{String(k + 1).padStart(2, "0")}</span>
              <div>
                <h2>{step.title}</h2>
                <p>{step.body.replace(/⌘|⇧/g, (m) => shortcut(m))}</p>
              </div>
            </li>
          ))}
        </ol>
        <section>
          <div className="sy-recent__head">
            <span className="sy-eyebrow">Tips for reliable sync</span>
          </div>
          <ul className="sy-learn__tips">
            {TIPS.map((tip) => (
              <li key={tip}>{tip}</li>
            ))}
          </ul>
        </section>
        <div className="sy-preset__actions">
          <Button variant="primary" size="36" icon={Plus} disabled={!ready} onClick={onCreate}>
            Create your first project
          </Button>
          <Button variant="secondary" size="36" onClick={() => useProd.getState().openSettings("shortcuts")}>
            Keyboard shortcuts
          </Button>
        </div>
      </div>
    </main>
  );
}
