import { usePick } from "../state/store";

export function StatusBar() {
  const { engine, project, timeline } = usePick("engine", "project", "timeline");
  const hello = engine.hello;
  const ffmpeg = hello?.ffmpeg?.match(/ffmpeg version (\S+)/)?.[1];
  return (
    <footer className="statusbar" data-testid="statusbar">
      <span className={`dot ${engine.state}`} title={`Engine ${engine.state}`} />
      <span>{hello ? `Engine ${hello.version}` : `Engine ${engine.state}`}</span>
      {hello && <span>{ffmpeg ? `FFmpeg ${ffmpeg}` : "FFmpeg missing"}</span>}
      {project && (
        <span className="path" title={project.path}>
          {project.path}
        </span>
      )}
      {timeline && (
        <span className="right" data-testid="sync-stats">
          {timeline.stats.clips} clips · {timeline.stats.synced} synced · {timeline.review.length} to review
        </span>
      )}
    </footer>
  );
}
