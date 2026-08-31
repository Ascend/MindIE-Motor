# Slurm 集群环境准备

本章用于准备运行 MindIE Motor 所需的 Slurm 集群环境。请按“安装软件 → 配置集群 → 启动并验证”的顺序执行，完成后再阅读[服务部署](./service_deployment.md)。

## 本章导航

- [1. 环境说明](#1-环境说明)
- [2. 安装 Slurm 及依赖](#2-安装-slurm-及依赖)
- [3. 配置集群基础服务](#3-配置集群基础服务)
- [4. 配置 Slurm 资源](#4-配置-slurm-资源)
- [5. 启动与验证](#5-启动与验证)
- [6. 安装 Apptainer](#6-安装-apptainer)
- [7. 下一步](#7-下一步)

## 1. 环境说明

主节点运行 `slurmctld`；计算节点运行 `slurmd`。主节点若参与计算，同时运行 `slurmd`。下列主机名、CPU、内存、卡数按实际环境替换。

全部节点使用同一份 `/etc/munge/munge.key`、`/etc/slurm/slurm.conf`、`/etc/slurm/gres.conf`、`/etc/slurm/cgroup.conf`。

## 2. 安装 Slurm 及依赖

### 2.1 安装系统依赖

所有节点：

```bash
dnf install -y \
    munge munge-libs munge-devel \
    gcc gcc-c++ make rpm-build \
    pam-devel readline-devel \
    hwloc hwloc-devel \
    json-c json-c-devel \
    libcurl libcurl-devel \
    openssl openssl-devel \
    mariadb-devel perl-devel
```

---

### 2.2 构建并安装 Slurm

在一台机器准备 `slurm-25.05.2.tar.bz2`：

```bash
rpmbuild -ta slurm-25.05.2.tar.bz2
```

RPM 生成在 `/root/rpmbuild/RPMS/$(uname -m)/`。

**主节点：**

```bash
dnf install -y \
    /root/rpmbuild/RPMS/$(uname -m)/slurm-25.05.2-1.$(uname -m).rpm \
    /root/rpmbuild/RPMS/$(uname -m)/slurm-slurmctld-25.05.2-1.$(uname -m).rpm
```

主节点参与计算时再安装：

```bash
dnf install -y \
    /root/rpmbuild/RPMS/$(uname -m)/slurm-slurmd-25.05.2-1.$(uname -m).rpm
```

**分发到计算节点：**

在构建机上将计算节点所需 RPM 拷到各节点 `/tmp`（主机名按实际替换，可对多节点循环执行）：

```bash
ARCH=$(uname -m)
RPMDIR=/root/rpmbuild/RPMS/${ARCH}
NODE=<compute-node>

scp "${RPMDIR}/slurm-25.05.2-1.${ARCH}.rpm" \
    "${RPMDIR}/slurm-slurmd-25.05.2-1.${ARCH}.rpm" \
    "root@${NODE}:/tmp/"
```

**计算节点安装：**

```bash
cd /tmp
dnf install -y \
    slurm-25.05.2-1.$(uname -m).rpm \
    slurm-slurmd-25.05.2-1.$(uname -m).rpm
```

---

## 3. 配置集群基础服务

### 3.1 创建用户和目录

所有节点：

```bash
getent group slurm >/dev/null || groupadd -r slurm

id slurm >/dev/null 2>&1 || \
    useradd -r -g slurm -d /var/lib/slurm -s /sbin/nologin slurm

mkdir -p /etc/slurm /var/spool/slurmctld /var/spool/slurmd /var/log/slurm

chown -R slurm:slurm /var/spool/slurmctld /var/log/slurm
chown -R root:root /var/spool/slurmd
```

---

### 3.2 配置 Munge

主节点：

```bash
mungekey --create
chown munge:munge /etc/munge/munge.key
chmod 400 /etc/munge/munge.key
systemctl enable --now munge
```

在主节点将密钥拷到各计算节点 `/tmp`：

```bash
NODE=<compute-node>
scp /etc/munge/munge.key "root@${NODE}:/tmp/munge.key"
```

各计算节点：

```bash
systemctl stop munge
cp /tmp/munge.key /etc/munge/munge.key
chown munge:munge /etc/munge/munge.key
chmod 400 /etc/munge/munge.key
systemctl enable --now munge
```

所有节点 `md5sum /etc/munge/munge.key` 须一致。

---

## 4. 配置 Slurm 资源

### 4.1 采集节点参数

每个运行 `slurmd` 的节点执行 `slurmd -C`，将 `CPUs`、`Boards`、`SocketsPerBoard`、`CoresPerSocket`、`ThreadsPerCore`、`RealMemory` 写入 `slurm.conf` 的 `NodeName` 行。

NPU 数量与型号：

```bash
npu-smi info
ls /dev/davinci[0-9]*
```

`/etc/hosts`（或 DNS）须能将节点地址解析为 `slurm.conf` 中的 `NodeName`。IPv6 集群请确认节点名和地址解析均支持 IPv6。

---

### 4.2 配置 slurm.conf

所有节点同一份 `/etc/slurm/slurm.conf`。以下示例以 **Atlas 800I A2（单机 8 卡）** 为例，其它机型按实际卡数修改 `Gres=npu:` 与 `gres.conf`：

```text
ClusterName=slurm-cluster
SlurmctldHost=<controller-hostname>

SlurmUser=slurm
SlurmdUser=root

AuthType=auth/munge
CredType=cred/munge

StateSaveLocation=/var/spool/slurmctld
SlurmdSpoolDir=/var/spool/slurmd

SlurmctldPidFile=/var/run/slurmctld.pid
SlurmdPidFile=/var/run/slurmd.pid

SlurmctldLogFile=/var/log/slurm/slurmctld.log
SlurmdLogFile=/var/log/slurm/slurmd.log

SwitchType=switch/none
MpiDefault=none

ProctrackType=proctrack/cgroup
TaskPlugin=task/cgroup,task/affinity

ReturnToService=2

SchedulerType=sched/backfill
SelectType=select/cons_tres
SelectTypeParameters=CR_Core
GresTypes=npu

NodeName=<compute-node-1> CPUs=<cpu-num> Boards=<boards> SocketsPerBoard=<sockets> CoresPerSocket=<cores> ThreadsPerCore=<threads> RealMemory=<memory-mb> Gres=npu:8 State=UNKNOWN
NodeName=<compute-node-2> CPUs=<cpu-num> Boards=<boards> SocketsPerBoard=<sockets> CoresPerSocket=<cores> ThreadsPerCore=<threads> RealMemory=<memory-mb> Gres=npu:8 State=UNKNOWN

# 分区名仅为示例, 请替换为实际的 Slurm 分区名.
PartitionName=<partition-name> Nodes=<compute-node-1>,<compute-node-2> Default=YES MaxTime=INFINITE State=UP
```

`Gres=npu:` 与该机卡数一致。`examples/slurm_deployer/deploy.sh` 中的 `PARTITION` 必须设置为上面配置的实际分区名。主节点参与计算时加入 `NodeName` 和分区 `Nodes=`。

同步到全部计算节点：

```bash
scp /etc/slurm/slurm.conf root@<compute-node>:/etc/slurm/slurm.conf
```

---

### 4.3 配置 gres.conf

所有节点 `/etc/slurm/gres.conf`（8 卡）：

```text
AutoDetect=off
Name=npu File=/dev/davinci[0-7]
```

卡数不是 8 时，同步修改区间与节点行 `Gres=npu:`。

---

### 4.4 配置 cgroup.conf

所有节点 `/etc/slurm/cgroup.conf`（cgroup v1）：

```text
CgroupPlugin=cgroup/v1
ConstrainDevices=yes
ConstrainCores=no
ConstrainRAMSpace=no
ConstrainSwapSpace=no
```

cgroup v2 环境将 `CgroupPlugin` 改为 `cgroup/v2`。

---

### 4.5 配置 NPU 设备权限

所有计算节点：

```bash
usermod -aG HwHiAiUser slurm
chmod 666 /dev/davinci[0-9]*
```

`/etc/udev/rules.d/99-davinci-slurm.rules`：

```text
KERNEL=="davinci[0-9]*", MODE="0666"
```

```bash
udevadm control --reload
udevadm trigger
```

---

## 5. 启动与验证

### 5.1 启动 Slurm 服务

主节点：

```bash
systemctl enable --now slurmctld
```

主节点参与计算，以及全部计算节点：

```bash
systemctl enable --now slurmd
```

修改配置后：先 `systemctl restart slurmctld`，再各节点 `systemctl restart slurmd`。

---

### 5.2 验证集群状态

```bash
scontrol ping
sinfo
sinfo -N -o "%N %G %T %P"
srun -N<节点数> -n<节点数> --ntasks-per-node=1 hostname
srun -N 1 -p <partition-name> --gres=npu:8 hostname
```

期望：`slurmctld` 正常；已配置的分区可用；GRES 为 `npu:<卡数>`；节点 `idle`。

```bash
squeue
scontrol show job <job-id>
journalctl -u slurmctld -n 100 --no-pager
journalctl -u slurmd -n 100 --no-pager
```

---

## 6. 安装 Apptainer

Motor 服务以 Apptainer 容器运行。建议在 Slurm 集群验证通过后安装 Apptainer 1.3.6，并将相同版本分发到所有参与调度的节点。

构建节点：

```bash
dnf groupinstall -y "Development Tools"

dnf install -y \
    rpm-build golang libseccomp-devel libtalloc-devel \
    libattr-devel protobuf-c-devel fuse3-devel \
    lzo-devel lz4-devel fakeroot cryptsetup wget git

cd /root
VERSION=1.3.6
wget https://github.com/apptainer/apptainer/releases/download/v${VERSION}/apptainer-${VERSION}.tar.gz

tar xvf apptainer-${VERSION}.tar.gz apptainer-${VERSION}/scripts
tar xvf apptainer-${VERSION}.tar.gz apptainer-${VERSION}/dist/rpm
cd apptainer-${VERSION}
./scripts/download-dependencies ..
cd ..
rm -rf apptainer-${VERSION}

rpmbuild -tb apptainer-${VERSION}.tar.gz
dnf install -y /root/rpmbuild/RPMS/$(uname -m)/apptainer-1.3.6-1.$(uname -m).rpm
apptainer version
```

使用非 SUID 包 `apptainer-1.3.6-1.<arch>.rpm`。将 RPM 分发到其余 Slurm 节点安装后：

```bash
srun -N<节点数> -n<节点数> --ntasks-per-node=1 \
    bash -c 'echo "$(hostname): $(apptainer version)"'
```

`.sif` 与模型权重须在各计算节点上路径一致。

## 7. 下一步

确认所有节点上的 `apptainer version` 一致，且 `.sif` 镜像和模型权重路径一致后，按[服务部署](./service_deployment.md)启动 Motor。
