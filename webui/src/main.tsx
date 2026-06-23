import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./index.css";
import { installAgoraCapture } from "./agoracap";

// Install BEFORE anything loads the Agora SDK, so we capture its signaling handshake for the native RE.
installAgoraCapture();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
