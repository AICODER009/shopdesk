import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// One self-contained IIFE the storefront drops in with a <script> tag. React is bundled
// in so the widget never fights the shop's own React version.
export default defineConfig({
  plugins: [react()],
  define: { "process.env.NODE_ENV": JSON.stringify("production") },
  build: {
    outDir: "../src/shopdesk/static/widget",
    emptyOutDir: true,
    lib: {
      entry: "src/embed.tsx",
      name: "Shopdesk",
      formats: ["iife"],
      fileName: () => "shopdesk-widget.js",
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["src/setup-tests.ts"],
  },
});
