#!/usr/bin/env bash
# Runbook S1, part 2: disable root and password SSH login. Run AS ROOT, ONLY AFTER you have
# confirmed in a second terminal that `ssh deploy@<ip>` works and `sudo -n true` succeeds.
# Keep your root session open until the final check passes.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 2; }

if [ ! -s /home/deploy/.ssh/authorized_keys ]; then
  echo "deploy has no authorized_keys: refusing, you would lock yourself out" >&2
  exit 1
fi

# "00-" sorts first and sshd uses the first value it sees, so this beats cloud-init's
# 50-cloud-init.conf (which may set PasswordAuthentication yes).
cat > /etc/ssh/sshd_config.d/00-hardening.conf <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
AllowUsers deploy
EOF

sshd -t   # abort (set -e) on a syntax error before touching the running daemon
systemctl restart ssh || systemctl restart sshd

echo "Effective settings:"
sshd -T | grep -Ei '^(permitrootlogin|passwordauthentication|kbdinteractiveauthentication|allowusers) '
echo
echo "Now, from ANOTHER terminal, check:"
echo "  ssh deploy@<ip>        -> works"
echo "  ssh root@<ip>          -> Permission denied"
echo "  ssh -o PubkeyAuthentication=no deploy@<ip> -> Permission denied"
