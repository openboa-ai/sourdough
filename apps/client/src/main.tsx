import "@openboa/yogurt/styles.css";
import "./styles/app.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Yogurt } from "@openboa/yogurt/yogurt";
import { App } from "./App";

const rootElement = document.getElementById("root");
if (!rootElement) {
  throw new Error("Missing #root mount container");
}

createRoot(rootElement).render(
  <StrictMode>
    <Yogurt appearance="auto" material="neutral">
      <App />
    </Yogurt>
  </StrictMode>
);
