import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./tokens.css";
import "./styles.css";
import '@fontsource/inter/400.css'
import '@fontsource/inter/500.css'
import '@fontsource/instrument-serif/400.css'

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
