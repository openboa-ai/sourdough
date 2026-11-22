"""Choose the product checks for the PR merge candidate."""

import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath


FLOW = re.compile(r"feat/(flow-[a-z0-9]+(?:-[a-z0-9]+)*)")


def repository_only(path):
    file = PurePosixPath(path)
    if file.parts[0] == ".github":
        return True
    if file.parts[0] == "docs":
        return file.suffix in {".md", ".sha256"}
    return len(file.parts) == 1 and (
        file.suffix == ".md" or file.name in {".editorconfig", ".gitattributes", ".gitignore"}
    )


def scope(base, head, same_repo, paths):
    if FLOW.fullmatch(base):
        if head.startswith("feat/flow-"):
            raise ValueError("Target main with a flow PR; target a flow with a unit PR.")
        return "unit"
    if base != "main":
        raise ValueError("Invalid flow target.")
    flow = FLOW.fullmatch(head)
    if flow and same_repo:
        return flow.group(1)
    if head.startswith("feat/flow-") or not all(map(repository_only, paths)):
        raise ValueError("Product changes reach main through a same-repository feat/flow-<slug> PR.")
    return "none"


if __name__ == "__main__":
    result = "none"
    if os.environ["GITHUB_EVENT_NAME"] == "pull_request":
        pr = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())["pull_request"]
        raw = subprocess.check_output(["git", "diff", "--name-only", "--no-renames", "-z", "HEAD^1", "HEAD", "--"])
        paths = [os.fsdecode(value) for value in raw.split(b"\0") if value]
        try:
            result = scope(pr["base"]["ref"], pr["head"]["ref"], pr["base"]["repo"]["id"] == pr["head"]["repo"]["id"], paths)
        except ValueError as error:
            raise SystemExit(str(error))
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        print(f"scope={result}", file=output)
