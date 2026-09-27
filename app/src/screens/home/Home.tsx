// S01 Home: start or resume work. Sidebar (brand, navigation, settings), hero row, recent projects.
import {
  BookOpen,
  Clapperboard,
  FolderOpen,
  House,
  Import,
  LayoutGrid,
  Plus,
  Settings,
  TriangleAlert,
} from "lucide-react";

import { KeyHint, SyncoraSymbol, shortcut } from "../../design-system/components";
import { useProd } from "../../state/production";
import { usePick } from "../../state/store";

function fileName(path: string): string {
  return (path.split(/[\\/]/).pop() ?? path).replace(/\.(syncora|mcsync)$/, "");
}

export function Home() {
  const { engine, recent, newProject, openProject } = usePick("engine", "recent", "newProject", "openProject");
  const ready = engine.state === "ready";
  const ffmpegMissing = ready && !engine.hello?.ffmpeg;

  return (
    <div className="sy-home" data-testid="welcome">
      <aside className="sy-home__side">
        <div className="sy-home__brand">
          <SyncoraSymbol size={22} />
          <span>Syncora</span>
        </div>
        {/* DESIGN-OPEN: #10 Projects/Presets/Learn pages are not drawn: Projects opens a project, the others are
            disabled with a tooltip (DESIGN_DECISIONS.md D-03). */}
        <nav className="sy-home__nav" aria-label="Home">
          <button type="button" className="sy-navitem" aria-current="page">
            <House size={16} aria-hidden /> Home
          </button>
          <button type="button" className="sy-navitem" onClick={() => void openProject()} disabled={!ready}>
            <Clapperboard size={16} aria-hidden /> Projects
          </button>
          <button type="button" className="sy-navitem" disabled title="Presets are not included in this version">
            <LayoutGrid size={16} aria-hidden /> Presets
          </button>
          <button
            type="button"
            className="sy-navitem"
            disabled
            title="Learning material is not included in this version"
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
    </div>
  );
}
