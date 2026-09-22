#!/usr/bin/env python3
"""Push a local directory to a GitHub repo branch via the git database API.

Usage:
    gh-push.py owner/repo branch /path/to/dir "commit message" [--sync]

Creates blobs for every file under the directory (skipping .git), builds a
tree, commits, and creates or updates the branch ref. Prints the commit SHA.

With --sync, files present in the repo but missing locally are deleted.
"""
import base64
import json
import os
import sys
import urllib.request
import urllib.error

sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
from dynamic_credentials import (
    add_surrogate_to_request,
    read_json_response,
    DynamicCredentialError,
)

API = "https://api.github.com"
CREDENTIAL = "custom.github"
ALLOWED = ["api.github.com"]


def api(method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    add_surrogate_to_request(
        req, CREDENTIAL, entry_name="access_token", allowed_hosts=ALLOWED
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return read_json_response(resp), resp.status
    except urllib.error.HTTPError as e:
        try:
            payload = read_json_response(e)
        except Exception:
            payload = {"message": f"HTTP {e.code}"}
        return payload, e.code


def seed_empty_repo(repo, branch):
    """Create an initial commit via the Contents API.

    The git database API refuses blob creation on a completely empty repo
    ("Git Repository is empty"), so seed one file first, then the normal
    push flow takes over.
    """
    content = base64.b64encode(
        b"# workout-app\n\nWorkout tracker: exercise library, logging, programs, progression.\n"
    ).decode("ascii")
    body = {"message": "Initial commit", "content": content, "branch": branch}
    payload, status = api("POST", f"/repos/{repo}/contents/README.md", body)
    # Contents API uses PUT; POST returns 404/405, so retry with PUT.
    if status in (404, 405):
        payload, status = api("PUT", f"/repos/{repo}/contents/README.md", body)
    if status not in (200, 201):
        print(f"seed commit failed: {payload}", file=sys.stderr)
        return False
    return True


def upload_blobs(repo, local_dir):
    """Upload every file under local_dir as a blob. Returns tree entries."""
    tree_entries = []
    for root, dirs, files in os.walk(local_dir):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in sorted(files):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, local_dir).replace(os.sep, "/")
            with open(full, "rb") as f:
                content = base64.b64encode(f.read()).decode("ascii")
            blob, s = api(
                "POST",
                f"/repos/{repo}/git/blobs",
                {"content": content, "encoding": "base64"},
            )
            if s == 409 and blob.get("message") == "Git Repository is empty.":
                return None  # signal: repo needs seeding
            if s not in (200, 201):
                print(f"blob failed for {rel}: {blob}", file=sys.stderr)
                return False
            tree_entries.append(
                {"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]}
            )
    return tree_entries


def repo_blob_paths(repo, tree_sha):
    """All blob paths in a tree (recursive)."""
    tree, s = api("GET", f"/repos/{repo}/git/trees/{tree_sha}?recursive=1")
    if s != 200:
        return set()
    return {t["path"] for t in tree.get("tree", []) if t.get("type") == "blob"}


def main() -> int:
    args = sys.argv[1:]
    sync = "--sync" in args
    args = [a for a in args if a != "--sync"]
    if len(args) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    repo, branch, local_dir, message = args
    if not os.path.isdir(local_dir):
        print(f"not a directory: {local_dir}", file=sys.stderr)
        return 2

    # Current tip of the branch, if any.
    ref, status = api("GET", f"/repos/{repo}/git/ref/heads/{branch}")
    parent_sha = ref["object"]["sha"] if status == 200 else None
    base_tree = None
    if parent_sha:
        commit, _ = api("GET", f"/repos/{repo}/git/commits/{parent_sha}")
        base_tree = commit["tree"]["sha"]

    # Upload blobs.
    tree_entries = upload_blobs(repo, local_dir)
    if tree_entries is None:
        # Empty repo: seed one commit via the Contents API, then retry.
        if not seed_empty_repo(repo, branch):
            return 1
        ref, status = api("GET", f"/repos/{repo}/git/ref/heads/{branch}")
        parent_sha = ref["object"]["sha"] if status == 200 else None
        if parent_sha:
            commit, _ = api("GET", f"/repos/{repo}/git/commits/{parent_sha}")
            base_tree = commit["tree"]["sha"]
        tree_entries = upload_blobs(repo, local_dir)
        if tree_entries is None or tree_entries is False:
            print("blob upload failed after seeding", file=sys.stderr)
            return 1
    if tree_entries is False:
        return 1
    if not tree_entries:
        print("nothing to push: no files found", file=sys.stderr)
        return 2

    if sync and base_tree:
        local_paths = {e["path"] for e in tree_entries}
        for stale in sorted(repo_blob_paths(repo, base_tree) - local_paths):
            tree_entries.append(
                {"path": stale, "mode": "100644", "type": "blob", "sha": None}
            )

    tree_body = {"tree": tree_entries}
    if base_tree:
        tree_body["base_tree"] = base_tree
    tree, s = api("POST", f"/repos/{repo}/git/trees", tree_body)
    if s not in (200, 201):
        print(f"tree failed: {tree}", file=sys.stderr)
        return 1

    commit_body = {"message": message, "tree": tree["sha"]}
    if parent_sha:
        commit_body["parents"] = [parent_sha]
    commit, s = api("POST", f"/repos/{repo}/git/commits", commit_body)
    if s not in (200, 201):
        print(f"commit failed: {commit}", file=sys.stderr)
        return 1

    if parent_sha:
        _, s = api(
            "PATCH",
            f"/repos/{repo}/git/refs/heads/{branch}",
            {"sha": commit["sha"]},
        )
    else:
        _, s = api(
            "POST",
            f"/repos/{repo}/git/refs",
            {"ref": f"refs/heads/{branch}", "sha": commit["sha"]},
        )
    if s not in (200, 201):
        print(f"ref update failed (status {s})", file=sys.stderr)
        return 1

    print(commit["sha"])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DynamicCredentialError as e:
        print(f"auth error: {e}", file=sys.stderr)
        sys.exit(1)
