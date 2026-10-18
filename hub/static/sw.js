// Service Worker v9 para Yedid Smart Hub (PWA)
// Resiliencia total contra cachés obsoletas y recuperación automática de clientes
const CACHE_NAME = 'yedid-hub-v9';
const ASSETS_TO_CACHE = [
  '/static/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png'
];

self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(ASSETS_TO_CACHE).catch((err) => {
        console.warn('Advertencia en precache inicial:', err);
      });
    })
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.map((key) => {
          if (key !== CACHE_NAME) {
            console.log('Purgando caché obsoleta de Yedid Hub:', key);
            return caches.delete(key);
          }
        })
      );
    }).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  // Las llamadas API siempre van directas a la red sin interceptación del Service Worker
  if (event.request.url.includes('/api/')) {
    return;
  }

  // Navegación (HTML principal): SIEMPRE a la red con bypass de caché HTTP.
  // NUNCA almacenar el HTML en CacheStorage para evitar atrapar a clientes PWA en versiones viejas.
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request, { cache: 'reload' })
        .then((response) => {
          return response;
        })
        .catch(() => {
          // Si verdaderamente no hay conexión de red con el host ni con Tailscale:
          return new Response(
            `<!DOCTYPE html>
            <html lang="es">
            <head>
              <meta charset="UTF-8">
              <meta name="viewport" content="width=device-width, initial-scale=1.0">
              <title>Yedid Smart Hub</title>
              <style>
                body { background: #0a0f1d; color: #f1f5f9; font-family: system-ui, -apple-system, sans-serif; display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100vh; margin: 0; padding: 20px; text-align: center; box-sizing: border-box; }
                .card { background: #131d31; border: 1px solid #26385a; border-radius: 16px; padding: 28px 20px; max-width: 400px; width: 100%; box-shadow: 0 10px 30px rgba(0,0,0,0.6); }
                h2 { color: #06b6d4; margin-top: 0; font-size: 1.25rem; }
                p { color: #94a3b8; font-size: 0.88rem; line-height: 1.5; margin: 14px 0 20px; }
                button { background: linear-gradient(135deg, #0284c7, #2563eb); color: white; border: none; padding: 12px 20px; border-radius: 10px; font-weight: 700; font-size: 0.95rem; cursor: pointer; width: 100%; }
              </style>
            </head>
            <body>
              <div class="card">
                <h2>⚠️ Sin Conexión con Yedid Hub</h2>
                <p>No se pudo conectar con el servidor local o la red Tailscale en este momento. Verifica tu conexión de red o MagicDNS.</p>
                <button onclick="window.location.reload()">Reintentar Conexión</button>
              </div>
            </body>
            </html>`,
            { headers: { 'Content-Type': 'text/html; charset=utf-8' } }
          );
        })
    );
    return;
  }

  // Assets estáticos (iconos, manifest) con fallback
  event.respondWith(
    caches.match(event.request).then((cached) => {
      return cached || fetch(event.request).then((response) => {
        if (response && response.status === 200) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
        }
        return response;
      });
    })
  );
});
