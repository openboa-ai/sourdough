"""Check contribution metadata supplied by GitHub, without reading intermediate commit messages."""

import json
import os
import re
import sys
from pathlib import Path

TYPES = "feat|fix|docs|refactor|test|ci|build|chore|perf|style|revert"
SUBJECT = re.compile(rf"(?:{TYPES})(?:\([^()\s]+\))?!?: [^\s][^\r\n]*")
TOPIC = re.compile(rf"(?:{TYPES}|codex)/[a-z0-9]+(?:-[a-z0-9]+)*")


def errors(event_name, event):
    if event_name == "push":
        commit = event.get("head_commit") or {}
        if event.get("ref") != "refs/heads/main" or not commit.get("id") or commit["id"] != event.get("after"):
            return ["Expected the resulting main commit in the push event."]
        subject = commit.get("message", "").partition("\n")[0]
        if not SUBJECT.fullmatch(subject):
            return ["The main commit subject must follow Conventional Commits, e.g. fix(queue): retry jobs."]
        return []

    if event_name != "pull_request":
        return ["Expected a pull_request or main push event."]

    pr = event["pull_request"]
    head = pr["head"]
    branch = head["ref"]
    author = pr["user"]
    dependabot_branch = (
        author.get("type") == "Bot"
        and author.get("login") == "dependabot[bot]"
        and head["repo"]["id"] == event["repository"]["id"]
        and branch.startswith("dependabot/")
        and len(branch) > len("dependabot/")
    )
    problems = []
    if not TOPIC.fullmatch(branch) and not dependabot_branch:
        problems.append("Use a lowercase topic branch, e.g. fix/42-retry-timeout.")
    if not SUBJECT.fullmatch(pr["title"]):
        problems.append("Use a conventional PR title, e.g. fix(queue): retry interrupted jobs.")
    return problems


if __name__ == "__main__":
    payload = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    problems = errors(os.environ["GITHUB_EVENT_NAME"], payload)
    for problem in problems:
        print(problem, file=sys.stderr)
    sys.exit(bool(problems))
