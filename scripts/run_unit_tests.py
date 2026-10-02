"""Run isolated tests; live HTTP smoke scripts require a configured server."""
from pathlib import Path
import subprocess
import sys


LIVE_SMOKE_TESTS = {
    "test_v1_chat_completions.py",
    "test_v1_images_edits.py",
    "test_v1_images_generations.py",
    "test_v1_messages.py",
    "test_v1_models.py",
    "test_v1_responses.py",
}


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    tests = sorted(
        str(path.relative_to(root))
        for path in (root / "test").glob("test_*.py")
        if path.name not in LIVE_SMOKE_TESTS
    )
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-q", *tests], cwd=root))
