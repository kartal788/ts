/*
 * sw.js — Yönetici paneli Web Push service worker'ı.
 *
 * İstekler sayfasından (veya panelin herhangi bir sayfasından) bildirimlere
 * izin verildiğinde bu dosya kayıt edilir (scope: "/"). Sunucu tarafında
 * yeni bir içerik veya abonelik talebi geldiğinde Backend/helper/webpush.py
 * bir Push mesajı gönderir; bu dosya o mesajı yakalayıp sistem bildirimi
 * olarak gösterir ve tıklanınca /istekler sayfasını açar.
 */

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('push', (event) => {
  let payload = { title: 'Yeni Talep', body: 'Yeni bir talep geldi.', url: '/istekler', tag: 'istek' };
  try {
    if (event.data) {
      payload = { ...payload, ...event.data.json() };
    }
  } catch (e) {
    // JSON değilse düz metin olarak göster
    if (event.data) payload.body = event.data.text();
  }

  const options = {
    body: payload.body,
    tag: payload.tag || 'istek',
    icon: payload.icon || undefined,
    badge: payload.badge || undefined,
    data: { url: payload.url || '/istekler' },
    renotify: true,
  };

  event.waitUntil(self.registration.showNotification(payload.title || 'Yeni Talep', options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const targetUrl = (event.notification.data && event.notification.data.url) || '/istekler';

  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        try {
          const clientUrl = new URL(client.url);
          if (clientUrl.pathname === targetUrl && 'focus' in client) {
            return client.focus();
          }
        } catch (e) { /* yoksay */ }
      }
      if (self.clients.openWindow) {
        return self.clients.openWindow(targetUrl);
      }
    })
  );
});
