import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// No dev proxy on purpose.  The UI talks to the API at an absolute URL so that
// "the API is unreachable" is a network failure the browser reports with the
// address it tried, rather than a 500 manufactured by a proxy.  See src/api.js.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5173, strictPort: true },
});
