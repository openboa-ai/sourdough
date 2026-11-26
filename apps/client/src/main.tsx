import "@openboa/yogurt/styles.css";
import "./styles/app.css";
import "./styles/graph.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";

const rootElement = document.getElementById("root");
if (!rootElement) {
  throw new Error("Missing #root mount container");
}

createRoot(rootElement).render(
  <StrictMode>
    <App />
  </StrictMode>
);
