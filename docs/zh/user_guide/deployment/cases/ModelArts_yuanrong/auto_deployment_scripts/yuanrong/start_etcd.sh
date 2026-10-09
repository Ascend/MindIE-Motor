#!/bin/bash

host_IP=$(hostname -I | xargs)
echo "host_IP = $host_IP"

export ETCD_IP="${host_IP}"
export ETCD_PORT=$1
export ETCD_PEER_PORT=12380
mkdir -p /mnt/cache/logs/etcd

etcd \
  --name etcd-single \
  --data-dir /mnt/cache/logs/etcd/etcd-data \
  --listen-client-urls http://0.0.0.0:${ETCD_PORT} \
  --advertise-client-urls http://${ETCD_IP}:${ETCD_PORT} \
  --listen-peer-urls http://0.0.0.0:${ETCD_PEER_PORT} \
  --initial-advertise-peer-urls http://${ETCD_IP}:${ETCD_PEER_PORT} \
  --initial-cluster etcd-single=http://${ETCD_IP}:${ETCD_PEER_PORT} \
  > /mnt/cache/logs/etcd/etcd.log 2>&1 &

sleep 3

etcdctl --endpoints "${ETCD_IP}:${ETCD_PORT}" put key "value"
etcdctl --endpoints "${ETCD_IP}:${ETCD_PORT}" get key
