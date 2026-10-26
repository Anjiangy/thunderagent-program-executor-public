#!/bin/bash
# M5 / route-B spike: can KV really move from card A to card B on this platform?
#
# Two vLLM instances, one card each, joined by MooncakeConnector over the NPU
# transfer engine, with the stock P/D proxy in front. If the decoder can answer
# a request it never prefilled, the block channel exists -- which is the whole
# prerequisite for block-level program migration.
#
#   bash scripts/pd_spike.sh            # launch, wait, probe, report
#   P_DISAGG_GPUS=0,1 bash ...          # override cards
#
# Everything lands in <server-dir>: prefill.log, decode.log, proxy.log, pd_spike.out.
set -x
exec >> <server-dir> 2>&1

source /usr/local/Ascend/ascend-toolkit/set_env.sh
source /usr/local/Ascend/cann-9.1.0/share/info/ascendnpu-ir/bin/set_env.sh
source /usr/local/Ascend/nnal/atb/set_env.sh --cxx_abi=1
export LD_LIBRARY_PATH=/usr/local/Ascend/cann-9.1.0/lib64:/usr/local/Ascend/cann-9.1.0/lib64/plugin/opskernel:/usr/local/Ascend/cann-9.1.0/lib64/plugin/nnengine:/usr/local/Ascend/cann-9.1.0/opp/built-in/op_impl/ai_core/tbe/op_tiling:/usr/local/Ascend/driver/lib64:/usr/local/Ascend/driver/lib64/common:/usr/local/Ascend/driver/lib64/driver:/usr/local/Ascend/nnal/atb/latest/atb/cxx_abi_1/lib:/usr/local/Ascend/nnal/atb/latest/atb/cxx_abi_1/examples:/usr/local/Ascend/nnal/atb/latest/atb/cxx_abi_1/tests/atbopstest:/usr/local/Ascend/ascend-toolkit/latest/tools/aml/lib64:/usr/local/Ascend/ascend-toolkit/latest/tools/aml/lib64/plugin:/usr/local/Ascend/ascend-toolkit/latest/lib64:/usr/local/Ascend/ascend-toolkit/latest/lib64/plugin/opskernel:/usr/local/Ascend/ascend-toolkit/latest/lib64/plugin/nnengine:/usr/local/Ascend/ascend-toolkit/latest/opp/built-in/op_impl/ai_core/tbe/op_tiling:/usr/local/python3.12.13/lib:/usr/local/lib:$LD_LIBRARY_PATH

MODEL=${MODEL:-<model-path>}
GPUS=${P_DISAGG_GPUS:-0,1}
PRE_GPU=${GPUS%,*}
DEC_GPU=${GPUS#*,}
PRE_PORT=${P_DISAGG_PREFILL_PORT:-8102}
DEC_PORT=${P_DISAGG_DECODE_PORT:-8103}
PROXY_PORT=${P_DISAGG_PROXY_PORT:-8104}
BOOTSTRAP_PORT=${P_DISAGG_BOOTSTRAP_PORT:-8998}
MAX_LEN=${P_DISAGG_MAX_LEN:-4096}
GPU_UTIL=${P_DISAGG_GPU_UTIL:-0.75}
EX=${P_DISAGG_EXAMPLES:-<refs>}
P_CFG='{"kv_connector":"MooncakeConnector","kv_role":"kv_producer"}'
D_CFG='{"kv_connector":"MooncakeConnector","kv_role":"kv_consumer"}'

mkdir -p <server-dir>
pkill -9 -f 'vllm serve' 2>/dev/null
pkill -9 -f mooncake_connector_proxy 2>/dev/null
sleep 8

echo "=== prefill: card $PRE_GPU :$PRE_PORT bootstrap :$BOOTSTRAP_PORT"
ASCEND_RT_VISIBLE_DEVICES=$PRE_GPU VLLM_MOONCAKE_BOOTSTRAP_PORT=$BOOTSTRAP_PORT \
  nohup vllm serve "$MODEL" --port "$PRE_PORT" --trust-remote-code --max-model-len "$MAX_LEN" \
  --gpu-memory-utilization "$GPU_UTIL" --enforce-eager --kv-transfer-config "$P_CFG" \
  > <server-dir> 2>&1 &

echo "=== decode: card $DEC_GPU :$DEC_PORT"
ASCEND_RT_VISIBLE_DEVICES=$DEC_GPU \
  nohup vllm serve "$MODEL" --port "$DEC_PORT" --trust-remote-code --max-model-len "$MAX_LEN" \
  --gpu-memory-utilization "$GPU_UTIL" --enforce-eager --kv-transfer-config "$D_CFG" \
  > <server-dir> 2>&1 &

for i in $(seq 1 120); do
  p=$(curl -s -o /dev/null -w '%{http_code}' localhost:$PRE_PORT/health 2>/dev/null)
  d=$(curl -s -o /dev/null -w '%{http_code}' localhost:$DEC_PORT/health 2>/dev/null)
  echo "t=${i} prefill=$p decode=$d"
  if [ "$p" = "200" ] && [ "$d" = "200" ]; then echo "READY after ~$((i*5))s"; break; fi
  [ "$i" = "120" ] && echo "TIMEOUT waiting for P/D"
  sleep 5
done

cd "$EX" || exit 1
nohup python3 mooncake_connector_proxy.py \
  --prefill "http://0.0.0.0:$PRE_PORT" "$BOOTSTRAP_PORT" \
  --decode "http://0.0.0.0:$DEC_PORT" \
  --port "$PROXY_PORT" > <server-dir> 2>&1 &
sleep 6

echo "=== connectors"
grep -hE 'Creating v1 connector|Mooncake Transfer Engine is using|Bootstrap Server started|Registering KV_Caches' \
  <server-dir> <server-dir> | sed 's/^.*\] //' | sort -u
echo "SPIKE_SETUP_DONE"
