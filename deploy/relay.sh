#!/usr/bin/env bash
# ============================================================================
#  TLS-passthrough relay (nginx, SNI-based) — for Hiddify panels, and OPTIONALLY
#  a co-located Hiddify Invoice panel on the SAME box.
#
#  What it does
#  ------------
#   • Listens on :443 and forwards each TLS connection UNCHANGED to the right
#     origin, chosen by SNI (the hostname the client asked for). It never
#     decrypts and never holds a certificate — every origin keeps using its own
#     cert. (This is why the relay needs no ACME/SSL setup of its own.)
#   • Listens on :80 and HTTP-proxies to the origin (for redirects + ACME
#     HTTP-01 challenges that the origins answer).
#   • If INVOICE_PANEL_DOMAIN is set, THAT one hostname is routed to the LOCAL
#     invoice panel instead of an external IP. The invoice installer publishes
#     Caddy on 127.0.0.1:8443 / :8080 (run it with BEHIND_RELAY=1); Caddy still
#     gets its OWN Let's Encrypt cert because TLS-ALPN-01 rides through the SNI
#     passthrough unchanged.
#
#  Run as root on the relay server:   bash relay.sh
#
#  Ordering when co-locating the invoice panel:
#    1) fill INVOICE_PANEL_DOMAIN below and run THIS script first,
#    2) then run the invoice installer with BEHIND_RELAY=1 DOMAIN=<same domain>.
#  (The relay must already be routing the domain so Caddy's first ACME succeeds.)
# ============================================================================
set -euo pipefail

# ---- 1. Panel passthrough map: <domain>  <origin-server-IP> -----------------
#         (edit these to your real panels; both :443 and :80 go to the same IP)
MAP="
panel-01.example.com   10.0.0.1
panel-02.example.com   10.0.0.2
"

# ---- 2. (Optional) co-located invoice panel --------------------------------
#  The hostname the invoice panel is served on. Its DNS A record must point at
#  THIS relay's public IP. Leave empty ("") to run a pure panel relay.
INVOICE_PANEL_DOMAIN=""
INVOICE_HTTPS_PORT="8443"   # must match CADDY_HTTPS_PUBLISH port in the installer
INVOICE_HTTP_PORT="8080"    # must match CADDY_HTTP_PUBLISH port in the installer

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root (sudo -i first)."
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y nginx libnginx-mod-stream curl openssl

[ -f /etc/nginx/nginx.conf.orig ] || cp /etc/nginx/nginx.conf /etc/nginx/nginx.conf.orig

S=""
H=""
while read -r d ip; do
  [ -z "${d:-}" ] && continue
  S="${S}        ${d} ${ip}:443;
"
  H="${H}        ${d} ${ip}:80;
"
done <<< "$MAP"

# Route the invoice panel's own domain to the LOCAL Caddy (localhost high ports).
if [ -n "${INVOICE_PANEL_DOMAIN:-}" ]; then
  S="${S}        ${INVOICE_PANEL_DOMAIN} 127.0.0.1:${INVOICE_HTTPS_PORT};
"
  H="${H}        ${INVOICE_PANEL_DOMAIN} 127.0.0.1:${INVOICE_HTTP_PORT};
"
fi

cat > /etc/nginx/nginx.conf <<NGINX
user www-data;
worker_processes auto;
pid /run/nginx.pid;
error_log /var/log/nginx/error.log warn;
include /etc/nginx/modules-enabled/*.conf;
events { worker_connections 8192; }

# TLS pass-through by SNI (no decryption, just forward to origin / local panel)
stream {
    map \$ssl_preread_server_name \$up443 {
${S}        default "";
    }
    server {
        listen 443 reuseport;
        listen [::]:443 reuseport;
        ssl_preread on;
        proxy_pass \$up443;
        proxy_connect_timeout 5s;
        proxy_timeout 1h;
    }
}

# Port 80 for redirects and Let's Encrypt (ACME) HTTP-01 to origin / local panel
http {
    map \$host \$up80 {
${H}        default "";
    }
    server {
        listen 80;
        listen [::]:80;
        location / {
            proxy_pass http://\$up80\$request_uri;
            proxy_set_header Host \$host;
            proxy_set_header X-Real-IP \$remote_addr;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto \$scheme;
        }
    }
}
NGINX

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow 80/tcp  || true
  ufw allow 443/tcp || true
fi

nginx -t
systemctl enable nginx >/dev/null 2>&1 || true
systemctl restart nginx

echo "-------- TEST (external panel origins) --------"
while read -r d ip; do
  [ -z "${d:-}" ] && continue
  if timeout 5 bash -c "</dev/tcp/${ip}/443" 2>/dev/null; then
    iss=$(echo | timeout 7 openssl s_client -connect "${ip}:443" -servername "${d}" 2>/dev/null \
          | openssl x509 -noout -issuer 2>/dev/null | sed 's/^issuer= *//' || true)
    code=$(curl -sk --resolve "${d}:443:127.0.0.1" "https://${d}/" -o /dev/null -w "%{http_code}" --max-time 12 || echo "000")
    echo "OK    ${d} -> ${ip} | via relay: HTTP ${code} | origin cert: ${iss:-unknown}"
  else
    echo "FAIL  ${d} -> ${ip} | relay cannot reach origin on 443 (firewall/security-group or origin down)"
  fi
done <<< "$MAP"
if [ -n "${INVOICE_PANEL_DOMAIN:-}" ]; then
  echo "LOCAL ${INVOICE_PANEL_DOMAIN} -> 127.0.0.1:${INVOICE_HTTPS_PORT} (invoice panel; run the installer with BEHIND_RELAY=1)"
fi
echo "----------------------------------------------"
