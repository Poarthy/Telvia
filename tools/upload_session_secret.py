"""Uploads the encrypted session (and the other Secrets) to the repo again, clears any previous
decrypt error on the bus, updates the workflows, and starts the Listener.

    python -m tools.upload_session_secret
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import protocol as P  # noqa: E402
from common.gitbus import GitBus  # noqa: E402
from local_server import config as C  # noqa: E402
from local_server.bootstrap import push_code, upload_secrets  # noqa: E402
from local_server.github_client import GitHub, GitHubError  # noqa: E402
from tools.login import ask_secret  # noqa: E402


def main() -> None:
    C.load_env()
    cfg = C.Config()
    if not cfg.configured:
        sys.exit("Finish the local server setup wizard first (no repo is defined yet).")
    key = C.ensure_content_key()
    passphrase = ask_secret("Session passphrase: ")
    owner, name = cfg["repo_owner"], cfg["repo_name"]
    gh = GitHub(cfg["github_token"])
    try:
        upload_secrets(gh, owner, name, passphrase=passphrase, content_key=key,
                       progress=lambda m: print("  " + m))
    except GitHubError as e:
        sys.exit(f"Error: {e.message}")

    try:
        push_code(cfg["github_token"], owner, name, cfg["instance_id"] or "sync", lambda m: print("  " + m))
    except Exception as e:  # noqa: BLE001
        print(f"  (Workflow update note: {e})")

    try:
        bus = GitBus(C.BUS_DIR, cfg.remote_url, cfg["github_token"])
        bus.init()
        bus.stage("d", P.SESSION_STATE)
        bus.stage("w", P.STATUS, P.dumps({"state": "idle", "detail": "", "ts": P.now()}))
        bus.stage("w", P.SESSION_REFRESHED, P.dumps({"ts": P.now()}))
        bus.flush("session refreshed")
    except Exception as e:  # noqa: BLE001
        print(f"  (Could not announce the refresh on the bus: {e})")

    try:
        if not gh.active_runs(owner, name):
            gh.dispatch(owner, name, ref=cfg["default_branch"] or "main")
            print("  Listener workflow dispatched on GitHub Actions ✅")
    except Exception as e:  # noqa: BLE001
        print(f"  (Could not dispatch workflow automatically: {e})")

    print("Done! Now run:  python -m local_server.main")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
