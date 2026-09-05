# Self-hosting

1. Copy `phase0a.env.example` to `.env` and fill every required value. Use your own HTTPS origin and VAPID contact address. Generate your own token, storage key and VAPID keys; never commit `.env`.
2. Provide a reachable MySQL 8.4 database and dedicated credentials. Apply `spikes/ios-web-push/scripts/migrations/*.sql` in filename order to that database before starting the server. MySQL is managed separately from this Compose stack.
3. Run `docker compose config --quiet`, then `docker compose up -d --build`. The server binds only to `127.0.0.1:8092`; Redis is internal. The named volume stores runtime data.
4. Adapt `nginx/relay.example.com.conf` to your own domain and certificate paths. Use the bootstrap example only during initial TLS setup. Check `nginx -t` before applying it. The examples contain shared rate-limit zones: install only one variant.
5. Verify `/readyz`, then pair your Android client and iPhone PWA using the same HTTPS origin. A healthy server does not prove notification or reply delivery on your devices.

Back up MySQL, the data volume and runtime keys securely. Keep `STORAGE_KEY` stable across restarts. This repository has no automatic production deployment workflow.
