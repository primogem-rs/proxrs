#!/usr/bin/env python3
"""Release RSS workload:16 concurrent slow readers,16MiB vs1GiB aggregate."""
import argparse
from proxy_cases import run_suite
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import threading
import time
from http_cases import connection
from proxy_cases import Fixture, ROOT


def workload(fixture, total):
    args = fixture.args()
    args[0] = os.environ.get('PROXRS_TEST_RELEASE_BINARY', str(ROOT / 'target/release/proxrs'))
    with fixture.ready(args) as process:
        barrier = threading.Barrier(16)
        def transfer(_):
            with closing(connection(process)) as client:
                barrier.wait(timeout=10)
                client.request('GET', f'http://origin.proxrs.test:8080/bytes?count={total // 16}')
                response = client.getresponse()
                assert response.status == 200
                # Every response is open before any reader drains its body.
                barrier.wait(timeout=10)
                received = 0
                while data := response.read(16384):
                    assert data == b'x' * len(data)
                    received += len(data)
                    time.sleep(.001)
                assert received == total // 16
        started = time.monotonic()
        progress = 0
        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(transfer, index) for index in range(16)]
            while not all(future.done() for future in futures):
                with closing(connection(process)) as client:
                    probe_started = time.monotonic()
                    client.request('GET', 'http://origin.proxrs.test:8080/')
                    assert client.getresponse().read() == b'proxrs-docker-origin\n'
                    assert time.monotonic() - probe_started < 3, 'healthy request stalled behind slow readers'
                    progress += 1
                time.sleep(.05)
            for future in futures:
                future.result()
        assert progress > 0
        elapsed = time.monotonic() - started
        process.terminate()
        # The owner alone reaps this invocation and caches its lifetime RSS.
        assert process.wait(5) == 0
        usage = process.usage
        result = dict(total_bytes=total, streams=16, read_bytes=16384, read_delay_seconds=.001,
                      peak_rss_bytes=usage.ru_maxrss, elapsed_seconds=round(elapsed, 3), healthy_requests=progress)
        print(json.dumps(result), flush=True)
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', default='ubuntu:24.04')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    assert os.uname().sysname == 'Darwin', 'RSS unit contract in this acceptance runner is macOS bytes'
    with Fixture(args.image) as fixture:
        small = workload(fixture, 16 * 1024**2)
        large = workload(fixture, 1024**3)
    delta = large['peak_rss_bytes'] - small['peak_rss_bytes']
    assert delta <= 64 * 1024**2, f'RSS grew by {delta} bytes'
    report = dict(image=args.image, platform='macOS', small=small, large=large, peak_rss_delta_bytes=delta)
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(f'PASS {args.image}: release RSS delta {delta / 1024**2:.2f}MiB <=64MiB', flush=True)


if __name__ == '__main__':
    run_suite(main)
