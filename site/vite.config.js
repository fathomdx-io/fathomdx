import { defineConfig } from 'vite';
import { resolve } from 'node:path';

// The placer (placer.html + src/placer/) is a dev-only tool for dropping
// waypoint pins onto the mind. It's intentionally absent from the prod
// build — the files stay in the repo and are reachable via `npm run dev`
// when we need to reposition waypoints.
export default defineConfig({
  build: {
    rollupOptions: {
      input: {
        main: resolve(import.meta.dirname, 'index.html'),
        mind: resolve(import.meta.dirname, 'mind.html'),
        download: resolve(import.meta.dirname, 'download.html'),
        contact: resolve(import.meta.dirname, 'contact.html'),
      },
    },
  },
});
