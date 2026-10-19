// Trading Agent service worker: makes the dashboard installable and gives a plain offline page. It stores NOTHING that
// holds money data: pages and every /api/ answer always go to the network (a failed page load gets the offline page);
// only static files (scripts, styles, fonts, icons) are kept. Bump VERSION when this file's rules change.
const VERSION = "v1";
const STATIC = "ta-static-" + VERSION;
const API = /^\/(demo\/|replay\/)?api\//;          // never intercepted: no respondWith, no cache
const STATIC_PATH = /^\/(static|fonts|icons)\//;
const OFFLINE = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#161826"><title>Trading Agent is offline</title>
<style>html,body{height:100%;margin:0;background:#161826;color:#e9e9ed;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{min-height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;padding:24px;text-align:center;box-sizing:border-box}
h1{font-size:20px;margin:0}p{margin:0;max-width:30em;line-height:1.5;color:#b9bbca}
button{min-height:48px;padding:0 28px;border-radius:12px;border:1px solid #2bb5a0;background:#2bb5a0;color:#161826;font-size:16px;font-weight:600}</style></head>
<body><main><h1>Trading Agent is offline</h1>
<p>Your phone cannot reach the server. Check that Tailscale is connected, then reload.</p>
<button type="button" onclick="location.reload()">Reload</button></main></body></html>`;

self.addEventListener("install", () => { self.skipWaiting(); });

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k.startsWith("ta-static-") && k !== STATIC).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;                                  // orders and every other POST go straight through
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (API.test(url.pathname)) return;                                 // the data: straight to the network, nothing stored
  if (req.mode === "navigate") {                                      // a page: network first, the offline page when it fails
    event.respondWith(fetch(req).catch(() => new Response(OFFLINE, {status: 503, headers: {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}})));
    return;
  }
  if (!STATIC_PATH.test(url.pathname)) return;                        // /sw.js, the manifest and anything else: the network
  if (/\.(js|css)$/.test(url.pathname)) {                             // scripts and styles: network first (they must match the page), the copy only when offline
    event.respondWith(fetch(req).then((res) => { if (res.ok) { const copy = res.clone(); caches.open(STATIC).then((c) => c.put(req, copy)); } return res; })
      .catch(() => caches.match(req).then((hit) => hit || Response.error())));
    return;
  }
  event.respondWith(caches.open(STATIC).then((cache) => cache.match(req).then((hit) => {   // fonts and icons: stale while revalidate
    const fresh = fetch(req).then((res) => { if (res.ok) cache.put(req, res.clone()); return res; }).catch(() => hit || Response.error());
    return hit || fresh;
  })));
});
