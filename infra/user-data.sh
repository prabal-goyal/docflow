#!/bin/bash
# First boot of the DocFlow server (Amazon Linux 2023). Installs Docker and the Compose plugin and
# prepares /opt/docflow. Application files arrive with each deploy, not here.
set -euxo pipefail

COMPOSE_VERSION=v5.7.1

dnf install -y docker
systemctl enable --now docker

# Docker Compose plugin from the official release, checked against its published checksum.
PLUGINS=/usr/libexec/docker/cli-plugins
mkdir -p "$PLUGINS"
BASE="https://github.com/docker/compose/releases/download/$COMPOSE_VERSION"
curl -fsSL "$BASE/docker-compose-linux-x86_64" -o /tmp/docker-compose
EXPECTED=$(curl -fsSL "$BASE/docker-compose-linux-x86_64.sha256" | awk '{print $1}')
echo "$EXPECTED  /tmp/docker-compose" | sha256sum -c -
install -m 0755 /tmp/docker-compose "$PLUGINS/docker-compose"

# 2 GiB of swap: the whole stack shares 2 GiB of RAM on a t3.small.
if [ ! -f /swapfile ]; then
  dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo "/swapfile none swap sw 0 0" >> /etc/fstab
fi

install -d -m 0700 /opt/docflow
