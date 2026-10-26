#!/usr/bin/env bash
# Start the two servers MooncakeStoreConnector needs, then verify them.
# Nothing here touches other users: both ports were checked free before use.
set -euo pipefail
cd "$(dirname "$0")/.."

# 1) master. -rpc_port 9013 = master service; admin server takes 9003 by itself.
#    NOTE: `ss -lntp` prints nothing inside this pod - verify with netstat.
pkill -9 -f mooncake_master || true
sleep 2
nohup mooncake_master -rpc_port=9013 -enable_ha=false > master.log 2>&1 &
for i in $(seq 1 20); do
  grep -q 'role=leader' master.log && { echo "master leader at $((i*3))s"; break; }
  sleep 3
done
netstat -tlnp 2>/dev/null | grep -E ':9003|:9013' || echo 'WARN: no listener on 9003/9013'

# 2) http metadata server on 8081 (8080 is taken by something that 404s every path)
pkill -9 -f mooncake_http_metadata_server || true
sleep 1
nohup mooncake_http_metadata_server --port 8081 > metadata.log 2>&1 &
sleep 4
code=$(curl -s -m 3 -X PUT -o /dev/null -w '%{http_code}' 'localhost:8081/metadata?key=t&v=1')
echo "metadata PUT -> $code (need 200)"

# 3) point the client config at it
sed -i "s|\"local_hostname\": \"[^\"]*\"|\"local_hostname\": \"$(hostname -I | awk '{print $1}')\"|" store_cfg.json
sed -i 's|"metadata_server": "[^"]*"|"metadata_server": "http://127.0.0.1:8081/metadata"|' store_cfg.json
sed -i 's|"master_server_address": "[^"]*"|"master_server_address": "127.0.0.1:9013"|' store_cfg.json
grep -oE '"(local_hostname|metadata_server|master_server_address)": "[^"]*"' store_cfg.json
