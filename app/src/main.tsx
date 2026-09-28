import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { applyTheme, savedTheme } from "./screens/settings/Settings";
import "./design-system/tokens.css";
import "./design-system/fonts.css";
import "./design-system/tokens-extra.css";
import "./design-system/base.css";
import "./shell/shell.css";
import "./screens/home/home.css";
import "./screens/media/media.css";
import "./screens/sync/sync.css";
import "./screens/analyze/analyze.css";
import "./screens/settings/settings.css";
import "./styles.css";

applyTheme(savedTheme());

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
