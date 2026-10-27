# Slurm Cluster Environment Preparation

This chapter prepares the Slurm cluster environment required to run MindIE Motor. Follow the order of "install software → configure the cluster → start and verify", and then read [Service Deployment](./service_deployment.md).

## Environment Description

The primary node runs `slurmctld`; the compute nodes run `slurmd`. If the primary node also performs computation, it runs `slurmd` as well. Replace the following host names, CPUs, memory, and card counts according to the actual environment.

All nodes use the same `/etc/munge/munge.key`, `/etc/slurm/slurm.conf`, `/etc/slurm/gres.conf`, and `/etc/slurm/cgroup.conf`.

## Installing Slurm and Dependencies

### Installing System Dependencies

On all nodes:

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

### Building and Installing Slurm

Prepare `slurm-25.05.2.tar.bz2` on one machine:

```bash
rpmbuild -ta slurm-25.05.2.tar.bz2
```

The RPMs are generated in `/root/rpmbuild/RPMS/$(uname -m)/`.

**Primary node:**

```bash
dnf install -y \
    /root/rpmbuild/RPMS/$(uname -m)/slurm-25.05.2-1.$(uname -m).rpm \
    /root/rpmbuild/RPMS/$(uname -m)/slurm-slurmctld-25.05.2-1.$(uname -m).rpm
```

If the primary node also performs computation, install additionally:

```bash
dnf install -y \
    /root/rpmbuild/RPMS/$(uname -m)/slurm-slurmd-25.05.2-1.$(uname -m).rpm
```

**Distributing to compute nodes:**

On the build machine, copy the RPMs required by compute nodes to `/tmp` on each node (replace the host name accordingly; this can be looped over multiple nodes):

```bash
ARCH=$(uname -m)
RPMDIR=/root/rpmbuild/RPMS/${ARCH}
NODE=<compute-node>

scp "${RPMDIR}/slurm-25.05.2-1.${ARCH}.rpm" \
    "${RPMDIR}/slurm-slurmd-25.05.2-1.${ARCH}.rpm" \
    "root@${NODE}:/tmp/"
```

**Installing on compute nodes:**

```bash
cd /tmp
dnf install -y \
    slurm-25.05.2-1.$(uname -m).rpm \
    slurm-slurmd-25.05.2-1.$(uname -m).rpm
```

## Configuring Basic Cluster Services

### Creating Users and Directories

On all nodes:

```bash
getent group slurm >/dev/null || groupadd -r slurm

id slurm >/dev/null 2>&1 || \
    useradd -r -g slurm -d /var/lib/slurm -s /sbin/nologin slurm

mkdir -p /etc/slurm /var/spool/slurmctld /var/spool/slurmd /var/log/slurm

chown -R slurm:slurm /var/spool/slurmctld /var/log/slurm
chown -R root:root /var/spool/slurmd
```

### Configuring Munge

Primary node:

```bash
mungekey --create
chown munge:munge /etc/munge/munge.key
chmod 400 /etc/munge/munge.key
systemctl enable --now munge
```

On the primary node, copy the key to `/tmp` on each compute node:

```bash
NODE=<compute-node>
scp /etc/munge/munge.key "root@${NODE}:/tmp/munge.key"
```

On each compute node:

```bash
systemctl stop munge
cp /tmp/munge.key /etc/munge/munge.key
chown munge:munge /etc/munge/munge.key
chmod 400 /etc/munge/munge.key
systemctl enable --now munge
```

The output of `md5sum /etc/munge/munge.key` must be the same on all nodes.

## Configuring Slurm Resources

### Collecting Node Parameters

On each node that runs `slurmd`, run `slurmd -C` and write `CPUs`, `Boards`, `SocketsPerBoard`, `CoresPerSocket`, `ThreadsPerCore`, and `RealMemory` into the `NodeName` line of `slurm.conf`.

Number and model of NPUs:

```bash
npu-smi info
ls /dev/davinci[0-9]*
```

`/etc/hosts` (or DNS) must be able to resolve the node address to the `NodeName` in `slurm.conf`. For an IPv6 cluster, confirm that both the node name and the address resolution support IPv6.

### Configuring slurm.conf

All nodes use the same `/etc/slurm/slurm.conf`. The following example uses an **Atlas 800I A2 (8 cards per node)**; for other models, change `Gres=npu:` and `gres.conf` according to the actual number of cards:

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

# The partition name is only an example. Replace it with the actual Slurm partition name.
PartitionName=<partition-name> Nodes=<compute-node-1>,<compute-node-2> Default=YES MaxTime=INFINITE State=UP
```

`Gres=npu:` must be consistent with the number of cards on the machine. When running `examples/deployer/slurm_deploy.py start`,
`--partition` must be set to the actual partition name configured above. If the primary node also performs computation, add it to
`NodeName` and to the partition `Nodes=`.

Synchronize to all compute nodes:

```bash
scp /etc/slurm/slurm.conf root@<compute-node>:/etc/slurm/slurm.conf
```

### Configuring gres.conf

`/etc/slurm/gres.conf` on all nodes (8 cards):

```text
AutoDetect=off
Name=npu File=/dev/davinci[0-7]
```

If the number of cards is not 8, change both the range and `Gres=npu:` in the node lines.

### Configuring cgroup.conf

`/etc/slurm/cgroup.conf` on all nodes (cgroup v1):

```text
CgroupPlugin=cgroup/v1
ConstrainDevices=yes
ConstrainCores=no
ConstrainRAMSpace=no
ConstrainSwapSpace=no
```

In a cgroup v2 environment, change `CgroupPlugin` to `cgroup/v2`.

### Configuring NPU Device Permissions

On all compute nodes:

```bash
usermod -aG HwHiAiUser slurm
chmod 666 /dev/davinci[0-9]*
```

`/etc/udev/rules.d/99-davinci-slurm.rules`:

```text
KERNEL=="davinci[0-9]*", MODE="0666"
```

```bash
udevadm control --reload
udevadm trigger
```

## Starting and Verification

### Starting Slurm Services

Primary node:

```bash
systemctl enable --now slurmctld
```

The primary node that also performs computation, and all compute nodes:

```bash
systemctl enable --now slurmd
```

After modifying the configuration, run `systemctl restart slurmctld` first, and then `systemctl restart slurmd` on each node.

### Verifying the Cluster Status

```bash
scontrol ping
sinfo
sinfo -N -o "%N %G %T %P"
srun -N<node count> -n<node count> --ntasks-per-node=1 hostname
srun -N 1 -p <partition-name> --gres=npu:8 hostname
```

Expected: `slurmctld` works normally; the configured partition is available; GRES is `npu:<card count>`; the nodes are `idle`.

```bash
squeue
scontrol show job <job-id>
journalctl -u slurmctld -n 100 --no-pager
journalctl -u slurmd -n 100 --no-pager
```

## Installing Apptainer

The Motor service runs as an Apptainer container. It is recommended to install Apptainer 1.3.6 after the Slurm cluster passes verification, and to distribute the same version to all nodes that participate in scheduling.

Build node:

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

Use the non-SUID package `apptainer-1.3.6-1.<arch>.rpm`. After distributing the RPM to the remaining Slurm nodes and installing it:

```bash
srun -N<node count> -n<node count> --ntasks-per-node=1 \
    bash -c 'echo "$(hostname): $(apptainer version)"'
```

The paths of the `.sif` file and the model weights must be the same on all compute nodes.

## Starting the Service

After confirming that `apptainer version` is the same on all nodes and that the `.sif` image and model weight paths are consistent, start Motor as described in [Service Deployment](./service_deployment.md).
