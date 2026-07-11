# install dependency，$MOTOR_SCRIPT_PATH由上层脚本start.sh传递，如果使用的是motor镜像，可以不传递该变量
pip install --no-index --find-links=$MOTOR_SCRIPT_PATH/packages -r $MOTOR_SCRIPT_PATH/configmap/requirements.txt
# install pciutils
dpkg -i $MOTOR_SCRIPT_PATH/pciutils-offline/*.deb
# install libzmq
dpkg -i $MOTOR_SCRIPT_PATH/libzmq-offline/*.deb
# install motor
pip install $MOTOR_SCRIPT_PATH/MindIE-PyMotor/dist/motor-0.1.0-py3-none-any.whl
echo "pymotor install succeed"
# copy conductor
cp $MOTOR_SCRIPT_PATH/mooncake_conductor /usr/local/bin/
# export env
export CONFIGMAP_PATH=$MOTOR_SCRIPT_PATH/configmap
export CONFIG_PATH=$MOTOR_SCRIPT_PATH/configmap
export ROLE=$1
export COORDINATOR_SERVICE=$2
export CONTROLLER_SERVICE=$3
export POD_IP=$4
export MOTOR_LOG_ROOT_PATH=/mnt/cache/logs/
export JOB_NAME=$5
export KV_CONDUCTOR_SERVICE=$6
export KVP_MASTER_SERVICE=$7
# yuanrong
export DS_WORKER_ADDR="${POD_IP}:18481"
unset GOOGLE_LOGTOSTDERR GOOGLE_ALSOLOGTOSTDERR
source $CONFIGMAP_PATH/boot.sh
echo "start boot.sh"
