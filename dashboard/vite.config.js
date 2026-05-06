import { defineConfig } from 'vite';

export default defineConfig({
  server: {
    port: 5173,
    proxy: {
      '/v1': 'http://localhost:8201',
      '/health': 'http://localhost:8201',
    },
  },
  plugins: [{
    name: 'ui-path-rewrite',
    configureServer(server) {
      // The production server mounts dashboard files at /ui/* — rewrite those
      // requests to the root so Vite can serve login.html, onboarding.html etc.
      server.middlewares.use((req, _res, next) => {
        if (req.url?.startsWith('/ui/')) {
          req.url = req.url.slice(3); // '/ui/login.html' → '/login.html'
        }
        next();
      });
    },
  }],
});
