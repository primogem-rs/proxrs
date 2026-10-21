#!/bin/bash
set -euo pipefail
umask 077

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for dependency in docker ssh openssl; do
    command -v "$dependency" >/dev/null || {
        echo "Missing dependency: $dependency" >&2
        exit 1
    }
done

docker_cmd=(docker)
"${docker_cmd[@]}" info >/dev/null
test_dir="$(mktemp -d "${TMPDIR:-/tmp}/proxrs-ssh.XXXXXX")"
container_id=""
base_image="${1:-ubuntu:24.04}"

record_fixture() {
    if [[ -n "${PROXRS_TEST_FIXTURES:-}" ]]; then
        python3 "$repo_root/tests/docker/fixture_record.py" "$@"
    fi
}

cleanup() {
    local result=$?
    trap - EXIT
    if [[ -n "$container_id" ]]; then
        if [[ "$result" -ne 0 ]]; then
            "${docker_cmd[@]}" logs "$container_id" >&2 || true
        fi
        if ! "${docker_cmd[@]}" rm -f "$container_id" >/dev/null; then
            echo "Could not remove test container: $container_id" >&2
            result=1
        else
            record_fixture removed "container_id=$container_id" || result=1
        fi
    fi
    rm -rf "$test_dir"
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"${docker_cmd[@]}" build --iidfile "$test_dir/image-id" --build-arg "BASE_IMAGE=$base_image" "$repo_root/tests/docker"
built_image="$(cat "$test_dir/image-id")"
record_fixture built "base_image=$base_image" "image_id=$built_image"
container_id="$("${docker_cmd[@]}" run --detach --rm \
    --label dev.proxrs.fixture=ssh \
    --publish 127.0.0.1::22 "$built_image")"
record_fixture created "container_id=$container_id" "image_id=$built_image"
ssh_port="$("${docker_cmd[@]}" port "$container_id" 22/tcp)"
ssh_port="${ssh_port##*:}"

# Disposable fixture password: pass it over stdin, never as a Docker argument.
openssl rand -hex 24 > "$test_dir/password"
{ printf 'proxrs:'; cat "$test_dir/password"; } \
    | "${docker_cmd[@]}" exec -i "$container_id" chpasswd

ready=false
for ((attempt = 0; attempt < 50; attempt++)); do
    if "${docker_cmd[@]}" exec "$container_id" test -s /run/sshd.pid; then
        ready=true
        break
    fi
    sleep 0.2
done
if [[ "$ready" != true ]]; then
    echo "Test SSH server did not become ready." >&2
    exit 1
fi

# Trust the fixture key obtained through Docker, not an unauthenticated scan.
host_key="$("${docker_cmd[@]}" exec "$container_id" cat /etc/ssh/ssh_host_ed25519_key.pub)"
printf '[127.0.0.1]:%s %s\n' "$ssh_port" "$host_key" > "$test_dir/known_hosts"
cat > "$test_dir/askpass" <<'ASKPASS'
#!/bin/sh
cat "$(dirname "$0")/password"
ASKPASS
chmod 700 "$test_dir/askpass"
export SSH_ASKPASS="$test_dir/askpass"
export SSH_ASKPASS_REQUIRE=force
export DISPLAY=proxrs-test

ssh_cmd=(ssh -F /dev/null -T -p "$ssh_port"
    -o "UserKnownHostsFile=$test_dir/known_hosts"
    -o GlobalKnownHostsFile=/dev/null
    -o StrictHostKeyChecking=yes
    -o PreferredAuthentications=password
    -o PubkeyAuthentication=no
    -o NumberOfPasswordPrompts=1
    -o ConnectTimeout=5
    -o ServerAliveInterval=5
    -o ServerAliveCountMax=2)

remote_user="$("${ssh_cmd[@]}" proxrs@127.0.0.1 id -un </dev/null)"
[[ "$remote_user" == proxrs ]]
echo "PASS: password SSH login to the Docker container."

# The origin is container-loopback-only and has no published Docker port.
printf 'GET / HTTP/1.0\r\nHost: localhost\r\n\r\n' \
    | "${ssh_cmd[@]}" -W 127.0.0.1:8080 proxrs@127.0.0.1 > "$test_dir/response"
if ! grep -q 'proxrs-docker-origin' "$test_dir/response"; then
    echo "Did not receive the test origin through SSH forwarding." >&2
    exit 1
fi
echo "PASS: HTTP response through an SSH direct-tcpip channel."

# Reject a wrong password without allowing a fallback authentication method.
printf 'intentionally-wrong-password\n' > "$test_dir/password"
if "${ssh_cmd[@]}" proxrs@127.0.0.1 true </dev/null 2> "$test_dir/auth-error"; then
    echo "SSH unexpectedly accepted an incorrect password." >&2
    exit 1
fi
if ! grep -q 'Permission denied' "$test_dir/auth-error"; then
    cat "$test_dir/auth-error" >&2
    exit 1
fi
echo "PASS: incorrect SSH password rejected."
echo "Fixture checks passed; run tests/docker/proxy_cases.py separately to validate proxrs."
