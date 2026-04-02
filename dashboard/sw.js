// Switchboard Service Worker — handles Web Push notifications

self.addEventListener('push', (event) => {
    if (!event.data) return;

    let payload;
    try {
        payload = event.data.json();
    } catch (e) {
        payload = { title: 'Ouvrage', body: event.data.text() };
    }

    const title = payload.title || 'Ouvrage';
    const options = {
        body: payload.body || '',
        tag: payload.tag || 'ouvrage',
        icon: '/dashboard/icon.png',
        badge: '/dashboard/icon.png',
        data: payload.data || {},
        requireInteraction: false,
    };

    event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
    event.notification.close();

    const url = event.notification.data && event.notification.data.url
        ? event.notification.data.url
        : '/dashboard';

    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windowClients) => {
            // Focus existing window if open
            for (const client of windowClients) {
                if (client.url.includes('/dashboard') && 'focus' in client) {
                    client.focus();
                    if ('navigate' in client) {
                        client.navigate(url);
                    }
                    return;
                }
            }
            // Otherwise open new window
            if (clients.openWindow) {
                return clients.openWindow(url);
            }
        })
    );
});
