FROM ubuntu:24.04

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        openssh-server python3 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash proxrs \
    && mkdir -p /run/sshd /srv/proxrs-test \
    && printf 'proxrs-docker-origin\n' > /srv/proxrs-test/index.html \
    && rm -f /etc/ssh/ssh_host_*

COPY sshd_config /etc/ssh/sshd_config
COPY entrypoint.sh /usr/local/bin/proxrs-test-entrypoint

EXPOSE 22
CMD ["sh", "/usr/local/bin/proxrs-test-entrypoint"]
