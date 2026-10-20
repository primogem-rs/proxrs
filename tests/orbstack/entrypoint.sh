#!/bin/sh
set -eu

# Generate fresh host keys per container, never in an image layer.
ssh-keygen -A >/dev/null 2>&1
python3 -m http.server 8080 --bind 127.0.0.1 --directory /srv/proxrs-test &
exec /usr/sbin/sshd -D -e
