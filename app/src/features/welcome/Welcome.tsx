import { usePick } from "../../state/store";

function fileName(path: string): string {
  return path.split(/[\\/]/).pop() ?? path;
}

export function Welcome() {
  const { engine, recent, newProject, openProject } = usePick("engine", "recent", "newProject", "openProject");
  const ready = engine.state === "ready";
  const ffmpegMissing = ready && !engine.hello?.ffmpeg;

  return (
    <main className="welcome" data-testid="welcome">
      <div className="welcome-card">
        <h1>Multicam Sync</h1>
        <p className="muted">
          Synchronise cameras and audio recorders by their sound and timecode, review anything uncertain, and send the
          timeline to DaVinci Resolve or Premiere Pro. Your original files are never modified.
        </p>
        <div className="welcome-actions">
          <button className="primary" onClick={() => void newProject()} disabled={!ready} data-testid="new-project">
            New project…
          </button>
          <button onClick={() => void openProject()} disabled={!ready} data-testid="open-project">
            Open project…
          </button>
        </div>
        {engine.state === "starting" && <p className="muted">Starting the sync engine…</p>}
        {engine.state === "crashed" && (
          <div className="notice error">
            <strong>The sync engine could not start.</strong>
            <pre>{engine.error}</pre>
          </div>
        )}
        {ffmpegMissing && (
          <div className="notice error">
            FFmpeg was not found, so media cannot be read. Reinstall Multicam Sync, or install FFmpeg and restart.
          </div>
        )}
        {recent.length > 0 && (
          <section className="recent">
            <h2>Recent projects</h2>
            <ul>
              {recent.map((path) => (
                <li key={path}>
                  <button className="link" onClick={() => void openProject(path)} disabled={!ready} title={path}>
                    {fileName(path)}
                  </button>
                  <span className="muted path">{path}</span>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </main>
  );
}
