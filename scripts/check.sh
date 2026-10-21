#!/bin/bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
python3 -m unittest discover -s tests/tooling -v
