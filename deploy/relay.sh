#!/usr/bin/env bash
# ============================================================================
#  Fixed TLS-passthrough relay (nginx, SNI-based).
#
#  Run this ONCE. You never edit it when adding, renaming, or moving the
#  co-located invoice panel — the only thing that is yours to keep current is the
#  panel MAP below (your Hiddify origins).
#
#  Routing:
#   • :443 — every TLS connection is forwarded UNCHANGED, chosen by SNI. A hostname
#     listed in MAP goes to its origin server; ANYTHING ELSE (i.e. the co-located
#     invoice panel's own domain, whatever it is) falls through to the LOCAL Caddy
#     on 127.0.0.1:8443. The relay never decrypts and holds no certificate.
#   • :80 — HTTP proxy for redirects + ACME HTTP-01. Same rule: MAP hosts go to
#     their origin; everything else to the local Caddy on 127.0.0.1:8080.
#
#  So the invoice panel just needs to be installed with BEHIND_RELAY=1 (its Caddy
#  binds 127.0.0.1:8080 / :8443); this relay's default route already points there.
#  There is NO invoice domain to configure here. Caddy still gets its own Let's
#  Encrypt cert — TLS-ALPN-01 rides through the SNI passthrough unchanged.
#
#  The same script is safe on relay boxes that DON'T host the panel: unknown
#  domains simply hit a local port with nothing listening and are refused.
#
#  Run as root:  bash relay.sh
# ============================================================================
set -euo pipefail

# ---- Panel passthrough map: <domain>  <origin-server-IP> -------------------
#      (edit these to your real panels; both :443 and :80 go to the same IP)
MAP="
panel-01.example.com   10.0.0.1
panel-02.example.com   10.0.0.2
"

# Co-located invoice panel (Caddy, published by the installer on these localhost
# ports via BEHIND_RELAY=1). Any domain NOT in MAP falls through to here.
LOCAL_HTTPS_UPSTREAM="127.0.0.1:8443"
LOCAL_HTTP_UPSTREAM="127.0.0.1:8080"

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

cat > /etc/nginx/nginx.conf <<NGINX
user www-data;
worker_processes auto;
pid /run/nginx.pid;
error_log /var/log/nginx/error.log warn;
include /etc/nginx/modules-enabled/*.conf;
events { worker_connections 8192; }

# TLS pass-through by SNI. Known panels → their origin; anything else → local panel.
stream {
    map \$ssl_preread_server_name \$up443 {
${S}        default ${LOCAL_HTTPS_UPSTREAM};
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

# Port 80: redirects + ACME HTTP-01. Known panels → origin; anything else → local panel.
http {
    map \$host \$up80 {
${H}        default ${LOCAL_HTTP_UPSTREAM};
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

echo "-------- TEST (panel origins) --------"
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
echo "DEFAULT (any other domain) -> ${LOCAL_HTTPS_UPSTREAM} : co-located invoice panel (install with BEHIND_RELAY=1)"
echo "--------------------------------------"
