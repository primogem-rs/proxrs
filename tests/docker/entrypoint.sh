#!/bin/sh
set -eu

# Generate fresh host keys per container, never in an image layer.
ssh-keygen -A >/dev/null 2>&1
openssl req -x509 -newkey rsa:2048 -nodes -keyout /run/test-ca.key -out /run/test-ca.pem -subj /CN=proxrs-test-ca -days 1 -addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign,cRLSign >/dev/null 2>&1
openssl req -newkey rsa:2048 -nodes -keyout /run/origin.key -out /run/origin.csr -subj /CN=origin.proxrs.test >/dev/null 2>&1
printf 'subjectAltName=DNS:origin.proxrs.test,IP:127.0.0.1,IP:::1\nextendedKeyUsage=serverAuth\nkeyUsage=critical,digitalSignature,keyEncipherment\nbasicConstraints=critical,CA:FALSE\n' > /run/origin.ext
openssl x509 -req -in /run/origin.csr -CA /run/test-ca.pem -CAkey /run/test-ca.key -CAcreateserial -out /run/origin.pem -days 1 -extfile /run/origin.ext >/dev/null 2>&1
python3 /usr/local/bin/proxrs-test-origins.py &
exec /usr/sbin/sshd -D -e
