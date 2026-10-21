"""Exercise the terminal-output policy through Cargo/Clippy's public CLI."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class TerminalOutputPolicy(unittest.TestCase):
    def test_output_outside_terminal_is_rejected(self):
        self.assert_rejected(
            'use std::println as output;\n'
            'fn main() { output!("misplaced diagnostic"); }\n'
        )

    def test_direct_output_handles_are_rejected(self):
        self.assert_rejected(
            'use std::io::stdout as output;\n'
            'fn main() { let _ = output(); let _ = std::io::stderr(); }\n'
        )

    def assert_rejected(self, source):
        with tempfile.TemporaryDirectory(prefix="proxrs-lint-") as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "Cargo.toml").write_text(
                '[package]\nname = "output-policy-probe"\nversion = "0.1.0"\nedition = "2024"\n'
            )
            config = ROOT / "clippy.toml"
            if config.exists():
                shutil.copyfile(config, root / "clippy.toml")
            (root / "src/main.rs").write_text(source)
            result = subprocess.run(
                ["cargo", "clippy", "--offline", "--", "-D", "warnings"],
                cwd=root, capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertIn("src/terminal.rs", result.stderr)


if __name__ == "__main__":
    unittest.main()
