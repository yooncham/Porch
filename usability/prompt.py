"""The exact prompt a fresh agent gets in the cold-start test: the task, then Porch's documentation.

    python3 usability/prompt.py RUN       writes usability/runs/RUN/prompt.txt and prints it
    python3 usability/prompt.py RUN --copy   also puts it on the clipboard (macOS), to paste in a chat

Nothing else is given: no call sequence, no device ids, no expected answer (docs/USABILITY_TEST.md).
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def prompt(task_file: str = "task.md") -> str:
    task = (REPO / "usability" / task_file).read_text().strip()
    documentation = (REPO / "docs" / "AGENTS.md").read_text().strip()
    return f"{task}\n\n---\n\n# Documentation\n\n{documentation}\n"


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    run = REPO / "usability" / "runs" / sys.argv[1]
    run.mkdir(parents=True, exist_ok=True)
    text = prompt()
    (run / "prompt.txt").write_text(text)
    if "--copy" in sys.argv:
        subprocess.run(["pbcopy"], input=text, text=True, check=True)
        print(f"The prompt ({len(text)} characters) is on the clipboard and in {(run / 'prompt.txt').relative_to(REPO)}.")
    else:
        print(text)


if __name__ == "__main__":
    main()
