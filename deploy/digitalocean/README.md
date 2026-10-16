# Deploy PaperPump on DigitalOcean (no SSH needed)

1. In DigitalOcean: **Create → Droplets**. Choose **Ubuntu 24.04 LTS**, **Basic, Regular, $6/mo (1 GB)**, any region.
2. Under **Advanced options → Add Initialization scripts (user data)**, paste the whole of `user-data.sh`.
3. Pick any authentication method (a password is fine; you won't SSH in). Click **Create Droplet**.
4. Wait about 5 minutes for setup to finish.
5. Open the Droplet → **Access → Launch Droplet Console** (log in as root).
6. Check setup finished: `tail -n 3 /var/log/paperpump-setup.log` should say "PaperPump setup complete".
7. Run `paperpump-set-token` and paste your Telegram bot token (nothing shows as you paste). Press Enter.
8. Run `cat /root/SITE_PASSWORD.txt` and save the website password somewhere safe.
9. Visit `https://YOUR-IP-WITH-DASHES.sslip.io` (example: IP 203.0.113.7 → https://203-0-113-7.sslip.io) and log in with that password.

Later:
- Update to the newest code: `paperpump-update`
- Logs: `journalctl -u paperpump-web -f` / `journalctl -u paperpump-bot -f`

## Custom domain (optional)

To serve the site from your own domain (e.g. `example.com`) instead of the sslip.io hostname:

1. In your DNS provider (e.g. Cloudflare), add an **A record**: name `@`, value = your Droplet's public IP (example: `203.0.113.7`). On Cloudflare, set Proxy status to **DNS only** (gray cloud) so Caddy can get its Let's Encrypt certificate directly.
2. In the Droplet console, replace `YOUR_DOMAIN` with your domain and run:

   ```sh
   sed -i 's/[0-9-]*\.sslip\.io/YOUR_DOMAIN/' /etc/caddy/Caddyfile && systemctl reload caddy
   sed -i 's#^BASE_URL=.*#BASE_URL=https://YOUR_DOMAIN#' /etc/paperpump.env && systemctl restart paperpump-web paperpump-bot
   ```

3. Wait a few minutes for DNS and the HTTPS certificate, then visit `https://YOUR_DOMAIN`. The bot's login links will now use the domain.
