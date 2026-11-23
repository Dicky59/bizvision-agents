#!/usr/bin/env bash
# One-time server preparation (runbook S1, part 1). Run AS ROOT on the fresh Hetzner VM:
#
#   scp scripts/server-bootstrap.sh root@5.222.8.228:/root/
#   ssh root@5.222.8.228 'bash /root/server-bootstrap.sh "$(cat)"' < ~/.ssh/id_ed25519.pub
#
# (the public key is passed on stdin -> $1). It does NOT touch sshd: lock SSH down with
# scripts/server-lockdown-ssh.sh only after you have verified `ssh deploy@...` works.
# Idempotent: safe to run twice.
set -euo pipefail

PUBKEY="${1:?usage: server-bootstrap.sh '<ssh public key line>'}"
case "$PUBKEY" in ssh-ed25519\ *|ssh-rsa\ *) ;; *) echo "not a public key line" >&2; exit 2 ;; esac
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 2; }

REPO_URL="https://github.com/Dicky59/bizvision-agents.git"
APP_DIR=/srv/bizvision-agents
export DEBIAN_FRONTEND=noninteractive

echo "==> packages, timezone"
apt-get update -y
apt-get full-upgrade -y
apt-get install -y ca-certificates curl git unattended-upgrades
timedatectl set-timezone UTC

echo "==> deploy user"
if ! id deploy >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" deploy
fi
usermod -aG sudo deploy
# Passwordless sudo: the account has no password and key-only SSH, and deploys need it.
echo 'deploy ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/90-deploy
chmod 440 /etc/sudoers.d/90-deploy
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
grep -qxF "$PUBKEY" /home/deploy/.ssh/authorized_keys 2>/dev/null \
  || echo "$PUBKEY" >> /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys
chmod 600 /home/deploy/.ssh/authorized_keys

echo "==> unattended security upgrades (reboot 04:00 UTC when needed)"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
cat > /etc/apt/apt.conf.d/52bv-unattended <<'EOF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:00";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
EOF

echo "==> Docker"
if ! command -v docker >/dev/null 2>&1; then
  . /etc/os-release
  DOCKER_REPO="https://download.docker.com/linux/ubuntu"
  if curl -fsI "$DOCKER_REPO/dists/$VERSION_CODENAME/Release" >/dev/null; then
    echo "Docker apt repo has a suite for $VERSION_CODENAME"
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL "$DOCKER_REPO/gpg" -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] $DOCKER_REPO $VERSION_CODENAME stable" \
      > /etc/apt/sources.list.d/docker.list
    apt-get update -y
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  else
    echo "No Docker apt suite for $VERSION_CODENAME yet: using Ubuntu's packages"
    apt-get install -y docker.io docker-compose-v2 docker-buildx
  fi
fi
install -d /etc/docker
if [ ! -f /etc/docker/daemon.json ]; then
  cat > /etc/docker/daemon.json <<'EOF'
{ "log-driver": "json-file", "log-opts": { "max-size": "10m", "max-file": "3" } }
EOF
fi
systemctl enable --now docker
systemctl restart docker
usermod -aG docker deploy   # root-equivalent; accepted on this one-person box

echo "==> app + backup directories"
install -d -o deploy -g deploy "$APP_DIR"
install -d -m 700 -o deploy -g deploy /var/backups/bizvision
if [ ! -d "$APP_DIR/.git" ]; then
  sudo -u deploy git clone "$REPO_URL" "$APP_DIR"
fi

echo
echo "Done. Docker: $(docker --version)"
echo "Next: open a SECOND terminal and check  ssh deploy@<ip>  and  sudo -n true  and  docker ps"
echo "      then run scripts/server-lockdown-ssh.sh as root."
