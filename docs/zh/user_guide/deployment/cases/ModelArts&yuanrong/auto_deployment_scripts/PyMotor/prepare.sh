EXAMPLES_PATH="/mnt/obs/scripts/PyMotor/examples" # 主机examples部署脚本路径
CONFIGMAP_PATH="/mnt/obs/scripts/PyMotor/configmap" # 服务启动脚本路径，需挂载到容器内
USER_CONFIG_PATH="/mnt/obs/scripts/PyMotor/examples/infer_engines/vllm/models/GLM/5/user_config.json" # user_config.json路径
ENV_PATH="/mnt/obs/scripts/PyMotor/examples/infer_engines/vllm/models/GLM/5/env.json" # env.json路径
mkdir -p $CONFIGMAP_PATH
# 容器启动脚本boot.sh，其运行时会调用startup目录下其他脚本，需要将其统一拷贝到$CONFIGMAP_PATH目录下。
cp -f $EXAMPLES_PATH/deployer/startup/boot.sh $CONFIGMAP_PATH/boot.sh
cp -f $EXAMPLES_PATH/deployer/startup/common.sh $CONFIGMAP_PATH/common.sh
cp -f $EXAMPLES_PATH/deployer/startup/hccl_tools.py $CONFIGMAP_PATH/hccl_tools.py
cp -f $EXAMPLES_PATH/deployer/startup/mooncake_config.py $CONFIGMAP_PATH/mooncake_config.py
cp -f $EXAMPLES_PATH/deployer/startup/roles/* $CONFIGMAP_PATH/

# 将准备好的user_config.json和env.json配置文件拷贝到$CONFIGMAP_PATH目录下
[ ! -f "$CONFIGMAP_PATH/user_config.json" ] && cp -f $USER_CONFIG_PATH $CONFIGMAP_PATH/user_config.json
[ ! -f "$CONFIGMAP_PATH/env.json" ] && cp -f $ENV_PATH $CONFIGMAP_PATH/env.json
# 若环境变量已加载，但发生改动，需先清理旧的环境变量。
sed -i '/^function set_controller_env()/,/^}/d' $CONFIGMAP_PATH/controller.sh
sed -i '/^function set_coordinator_env()/,/^}/d' $CONFIGMAP_PATH/coordinator.sh
sed -i '/^function set_prefill_env()/,/^}/d' $CONFIGMAP_PATH/engine.sh
sed -i '/^function set_decode_env()/,/^}/d' $CONFIGMAP_PATH/engine.sh
sed -i '/^function set_common_env()/,/^}/d' $CONFIGMAP_PATH/common.sh
sed -i '/^function set_kv_pool_env()/,/^}/d' $CONFIGMAP_PATH/kv_pool.sh
sed -i '/^function set_kv_conductor_env()/,/^}/d' $CONFIGMAP_PATH/kv_conductor.sh
sed -i '/^function set_controller_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/^function set_coordinator_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/^function set_prefill_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/^function set_decode_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/^function set_kv_pool_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/^function set_kv_conductor_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/./,$!d' $CONFIGMAP_PATH/common.sh

# 加载user_config.json和env.json中的环境变量，并作用于容器启动脚本。
python $EXAMPLES_PATH/deployer/startup/set_env_docker.py --configmap_path $CONFIGMAP_PATH
