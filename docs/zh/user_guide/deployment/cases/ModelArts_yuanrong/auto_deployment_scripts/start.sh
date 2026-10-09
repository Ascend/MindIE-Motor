set +x
echo "server start!"

## 下载权重
mkdir -p /mnt/cache
mkdir -p /workspace/scripts
SCRIPT_PATH=/workspace/scripts
cp -rf /mnt/obs/scripts/* $SCRIPT_PATH

MOTOR_SCRIPT_PATH="$SCRIPT_PATH/PyMotor"

WEIGHTS_DIR="/mnt/cache/GLM-5.1-W8A8"
if [ ! -d "$WEIGHTS_DIR" ];then
    echo "下载权重"
    # 下载模型权重到/mnt/cache/
    mv /mnt/obs/GLM-5.1-W8A8/* $WEIGHTS_DIR/
    du -s $WEIGHTS_DIR
    df -h
else
    echo "权重目录已存在"
fi

## vllm0.18.0版本需要打补丁mooncake_connector.py，修复glm5.1精度问题

## 更新生成ranktable.json
MS_GLOBAL_RANKTABLE_TABLE=/user/global/config/global_rank_table.json

MAX_WAIT_S="${MAX_WAIT_S:-600}"
elapsed_s=0
while true; do
    json_string=$(cat $MS_GLOBAL_RANKTABLE_TABLE)
    echo $json_string;
    RESULT=$(jq -r '.status' $MS_GLOBAL_RANKTABLE_TABLE 2>/dev/null)
    echo $RESULT
    if [[ $RESULT = "completed" ]]; then
        echo "MA ranktable is completed";
        break;
    fi
    if ((elapsed_s >= MAX_WAIT_S)); then
        echo "MA ranktable not completed after ${MAX_WAIT_S}s" >&2
        exit 1
    fi
    echo "waiting for MA ranktable... elapsed ${elapsed_s}s, max ${MAX_WAIT_S}s"
    sleep 1;
    elapsed_s=$((elapsed_s + 1))
done;

host_IP=$(hostname -I | xargs)
echo "host_IP = $host_IP"

### check port
netstat -anop

### 启动元戎
mkdir -p /mnt/cache/logs
bash $SCRIPT_PATH/yuanrong/start_base_env.sh

NODE_IPS=$(jq -r '.server_group_list[].server_list[].server_ip' "$MS_GLOBAL_RANKTABLE_TABLE")
echo $NODE_IPS
IFS=$'\n' read -r -d '' -a ip_array <<< "$NODE_IPS"
P0="${ip_array[0]}"
P1="${ip_array[1]}"
P2="${ip_array[2]}"
P3="${ip_array[3]}"
D0="${ip_array[4]}"
D1="${ip_array[5]}"
D2="${ip_array[6]}"
D3="${ip_array[7]}"

echo "P0 = $P0"
echo "P1 = $P1"
echo "P2 = $P2"
echo "P3 = $P3"
echo "D0 = $D0"
echo "D1 = $D1"
echo "D2 = $D2"
echo "D3 = $D3"

## 拉起服务
source $MOTOR_SCRIPT_PATH/prepare.sh

LOG_DIR=/mnt/cache/logs/motor/$(date +%Y-%m-%d_%H-%M-%S)/
mkdir -p ${LOG_DIR}

echo "LOG_DIR: ${LOG_DIR}"

## 设置推理引擎负载均衡调用端口为1028(即不使用motor亲和调度)
if [ "$host_IP" = "$P0" ]; then
    echo "this is p0 and proxy"
    echo "start proxy"
    nohup python $SCRIPT_PATH/load_balance_proxy_server_example.py \
        --port 1028 \
        --host 0.0.0.0 \
        --prefiller-hosts $P0 $P1 $P2 $P3 \
        --prefiller-ports 10000 10000 10000 10000 \
        --decoder-hosts $D0 $D0 $D1 $D1 $D2 $D2 $D3 $D3 \
        --decoder-ports 10000 10002 10000 10002 10000 10002 10000 10002 \
        2>&1 | tee ${LOG_DIR}/proxy.log &
fi

## 拉起motor及推理实例
if [ "$host_IP" = "$P0" ]; then
    echo "p0 & coordinator"
    source $MOTOR_SCRIPT_PATH/start_motor.sh coordinator $P0 $P1 $P0 "coordinator" $P2 $P3 2>&1 | tee ${LOG_DIR}/coordinator.log &
    source $MOTOR_SCRIPT_PATH/start_motor.sh prefill $P0 $P1 $P0 "instance-p0" $P2 $P3 2>&1 | tee ${LOG_DIR}/p0.log
elif [ "$host_IP" = "$P1" ]; then
    echo "p1 & controller"
    source $MOTOR_SCRIPT_PATH/start_motor.sh controller $P0 $P1 $P1 "controller" $P2 $P3 2>&1 | tee ${LOG_DIR}/controller.log &
    source $MOTOR_SCRIPT_PATH/start_motor.sh prefill $P0 $P1 $P1 "instance-p0" $P2 $P3 2>&1 | tee ${LOG_DIR}/p1.log
elif [ "$host_IP" = "$P2" ]; then
    echo "p2 & kv_conductor"
    source $MOTOR_SCRIPT_PATH/start_motor.sh kv_conductor $P0 $P1 $P2 "kv_conductor" $P2 $P3 2>&1 | tee ${LOG_DIR}/kv_conductor.log &
    source $MOTOR_SCRIPT_PATH/start_motor.sh prefill $P0 $P1 $P2 "instance-p0" $P2 $P3 2>&1 | tee ${LOG_DIR}/p2.log
elif [ "$host_IP" = "$P3" ]; then
    echo "p3"
    source $MOTOR_SCRIPT_PATH/start_motor.sh prefill $P0 $P1 $P3 "instance-p0" $P2 $P3 2>&1 | tee ${LOG_DIR}/p3.log
elif [ "$host_IP" = "$D0" ]; then
    echo "d0"
    source $MOTOR_SCRIPT_PATH/start_motor.sh decode $P0 $P1 $D0 "instance-d0" $P2 $P3 2>&1 | tee ${LOG_DIR}/d0.log
elif [ "$host_IP" = "$D1" ]; then
    echo "d1"
    source $MOTOR_SCRIPT_PATH/start_motor.sh decode $P0 $P1 $D1 "instance-d0" $P2 $P3 2>&1 | tee ${LOG_DIR}/d1.log
elif [ "$host_IP" = "$D2" ]; then
    echo "d2"
    source $MOTOR_SCRIPT_PATH/start_motor.sh decode $P0 $P1 $D2 "instance-d0" $P2 $P3 2>&1 | tee ${LOG_DIR}/d2.log
elif [ "$host_IP" = "$D3" ]; then
    echo "d3"
    source $MOTOR_SCRIPT_PATH/start_motor.sh decode $P0 $P1 $D3 "instance-d0" $P2 $P3 2>&1 | tee ${LOG_DIR}/d3.log
fi

echo "server start end!"
sleep 36000000
