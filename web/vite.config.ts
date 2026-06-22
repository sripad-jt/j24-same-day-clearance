import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  // Served under /j24-clearance/ behind the parallax nginx proxy (which strips
  // the prefix before forwarding to this container's nginx). Mirrors the
  // sibling j24-store-vision app served under /j24-vision/.
  base: "/j24-clearance/",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
});
