// Basic-auth gate for the preview deploy. Password is set via the
// SITE_PASSWORD env var on the Cloudflare Pages project (Settings →
// Environment variables → Production + Preview). USERNAME defaults to
// "fathom" but can also be overridden via SITE_USERNAME.
export async function onRequest({ request, env, next }) {
  const USERNAME = env.SITE_USERNAME || "fathom";
  const PASSWORD = env.SITE_PASSWORD || "change-me";

  const auth = request.headers.get("Authorization") || "";
  const [scheme, encoded] = auth.split(" ");

  if (scheme === "Basic" && encoded) {
    try {
      const decoded = atob(encoded);
      const ix = decoded.indexOf(":");
      const user = decoded.slice(0, ix);
      const pass = decoded.slice(ix + 1);
      if (user === USERNAME && pass === PASSWORD) {
        return next();
      }
    } catch (_) { /* fall through to 401 */ }
  }

  return new Response("Authentication required.", {
    status: 401,
    headers: {
      "WWW-Authenticate": 'Basic realm="Fathom preview", charset="UTF-8"',
      "Content-Type": "text/plain",
    },
  });
}
