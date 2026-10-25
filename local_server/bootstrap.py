"""Automatic repo setup: find/create the repo, upload stripped code, create the bus branch, set the Secrets."""
from __future__ import annotations

import ast
import hashlib
import os
import secrets
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from common import protocol as P
from common.crypto import CryptoError, decrypt_with_passphrase, key_to_b64
from common.gitbus import GitBus, make_env, run_git
from local_server import config as C
from local_server.github_client import REPO_PREFIX, GitHub, GitHubError

Progress = Callable[[str], None]


def code_fingerprint() -> str:
    """Computes a fast SHA-256 fingerprint of local bot/, common/ and .github/workflows/ files."""
    h = hashlib.sha256(P.CODE_VERSION.encode("utf-8"))
    for folder in ("bot", "common", ".github/workflows"):
        base = C.PROJECT_ROOT / folder
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith((".pyc", ".tmp~")):
                try:
                    h.update(str(p.relative_to(C.PROJECT_ROOT)).replace("\\", "/").encode("utf-8"))
                    h.update(p.read_bytes())
                except OSError:
                    pass
    return h.hexdigest()[:32]


def _strip_docstrings(node: ast.AST) -> None:
    for child in ast.walk(node):
        if isinstance(child, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if (child.body and isinstance(child.body[0], ast.Expr)
                    and isinstance(child.body[0].value, ast.Constant)
                    and isinstance(child.body[0].value.value, str)):
                child.body.pop(0)
                if not child.body:
                    child.body.append(ast.Pass())


def _clean_py_source(src_text: str) -> str:
    """Strips comments and docstrings from Python files before pushing to main."""
    try:
        tree = ast.parse(src_text)
        _strip_docstrings(tree)
        return ast.unparse(tree) + "\n"
    except Exception:  # noqa: BLE001
        return src_text


def _copy_and_clean_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.tmp~"), dirs_exist_ok=True)
    for py_file in dst.rglob("*.py"):
        try:
            raw = py_file.read_text(encoding="utf-8")
            py_file.write_text(_clean_py_source(raw), encoding="utf-8")
        except OSError:
            pass


# -- Code on main --------------------------------------------------------------------------------
def push_code(token: str, owner: str, name: str, instance_id: str, progress: Progress) -> bool:
    """Puts stripped bot/, common/ and the workflows on main. Returns True if anything changed."""
    env = make_env(token, author_name=owner, author_email=f"{owner}@users.noreply.github.com")
    url = f"https://github.com/{owner}/{name}.git"
    tmp = Path(tempfile.mkdtemp(prefix="tgb-code-"))
    try:
        run_git(["init", "-q"], tmp, env)
        run_git(["symbolic-ref", "HEAD", "refs/heads/main"], tmp, env)
        run_git(["remote", "add", "origin", url], tmp, env)
        r = run_git(["fetch", "-q", "--depth=1", "origin", "main"], tmp, env, check=False)
        if r.returncode == 0:
            run_git(["reset", "-q", "--hard", "FETCH_HEAD"], tmp, env)
        for d in ("bot", "common", ".github"):
            shutil.rmtree(tmp / d, ignore_errors=True)
        _copy_and_clean_tree(C.PROJECT_ROOT / "bot", tmp / "bot")
        _copy_and_clean_tree(C.PROJECT_ROOT / "common", tmp / "common")
        shutil.copytree(
            C.PROJECT_ROOT / ".github" / "workflows",
            tmp / ".github" / "workflows",
            dirs_exist_ok=True,
        )
        (tmp / P.META_FILE).write_bytes(P.dumps({"instance_id": instance_id, "version": P.CODE_VERSION}))
        (tmp / "README.md").write_text(f"# {name}\n\nWorkspace state synchronization.\n", encoding="utf-8")
        run_git(["add", "-A"], tmp, env)
        if run_git(["diff", "--cached", "--quiet"], tmp, env, check=False).returncode == 0:
            progress("The code in the repo is up to date.")
            return False
        run_git(["commit", "-q", "-m", f"build {P.CODE_VERSION}"], tmp, env)
        progress("Uploading the code to the repo…")
        run_git(["push", "-q", "origin", "HEAD:refs/heads/main"], tmp, env, timeout=600)
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- bus branch ----------------------------------------------------------------------------------
def init_bus_branch(cfg: C.Config, token: str, owner: str, name: str, progress: Progress) -> None:
    bus = GitBus(Path(tempfile.mkdtemp(prefix="tgb-bus-")), f"https://github.com/{owner}/{name}.git", token)
    try:
        exists = bus.init()
        if exists:
            progress("Updating the bus branch status…")
        else:
            progress("Creating the bus branch…")
            bus.stage("w", ".gitattributes", b"* -delta -text\n")
            bus.stage("w", "commands/.keep", b"")
        bus.stage("w", P.CONFIG, P.dumps({**P.DEFAULT_BUS_CONFIG, **cfg.bus_config()}))
        bus.stage("w", P.STATUS, P.dumps({"state": "idle", "detail": "", "ts": P.now()}))
        bus.stage("d", P.SESSION_STATE)
        bus.flush("init")
    finally:
        shutil.rmtree(bus.dir, ignore_errors=True)


# -- Secrets -------------------------------------------------------------------------------------
def upload_secrets(gh: GitHub, owner: str, name: str, *, passphrase: str, content_key: bytes,
                   progress: Progress) -> None:
    try:
        session_enc = C.SESSION_ENC_PATH.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise GitHubError(0, "session.enc was not found. Run \"python -m tools.login\" first.") from e
    try:
        decrypt_with_passphrase(session_enc, passphrase)
    except CryptoError as e:
        raise GitHubError(0, "Wrong passphrase (it cannot open session.enc).") from e
    api_id, api_hash = os.environ.get("TG_API_ID", ""), os.environ.get("TG_API_HASH", "")
    if not api_id.isdigit() or not api_hash:
        raise GitHubError(0, "TG_API_ID / TG_API_HASH are not set in the .env file.")
    values = {
        "TG_SESSION_ENC": session_enc,
        "TG_SESSION_KEY": passphrase,
        "TG_API_ID": api_id,
        "TG_API_HASH": api_hash,
        "TG_CONTENT_KEY": key_to_b64(content_key),
    }
    for k, v in values.items():
        gh.put_secret(owner, name, k, v)
        progress(f"Secret {k} set.")


# -- Actions -------------------------------------------------------------------------------------
def ensure_actions(gh: GitHub, owner: str, name: str, progress: Progress) -> None:
    if not gh.actions_enabled(owner, name):
        progress("Actions was disabled on the repo; enabling it…")
        gh.enable_actions(owner, name)
    for wf in gh.list_workflows(owner, name):
        if wf.get("state", "active") != "active":
            progress(f"Workflow \"{wf.get('name')}\" was disabled; enabling it…")
            try:
                gh.enable_workflow(owner, name, wf["id"])
            except GitHubError:
                progress("  (Enabling the workflow failed; enable it manually from the GitHub Actions tab.)")


# -- Full procedure ------------------------------------------------------------------------------
def run_bootstrap(cfg: C.Config, *, visibility: str = "private", repo_name: str | None = None,
                  existing_repo: str | None = None, passphrase: str = "", encrypt: bool = True,
                  progress: Progress = print) -> dict:
    if visibility not in ("private", "public"):
        raise GitHubError(0, "visibility must be private or public.")
    if not passphrase:
        raise GitHubError(0, "Enter your session passphrase so the encrypted session and Secrets can be uploaded.")
    try:
        session_enc = C.SESSION_ENC_PATH.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise GitHubError(0, "session.enc was not found. Run \"python -m tools.login\" first.") from e
    try:
        decrypt_with_passphrase(session_enc, passphrase)
    except CryptoError as e:
        raise GitHubError(0, "Wrong passphrase (it cannot decrypt session.enc).") from e

    if visibility == "public":
        encrypt = True
        progress("The repo is public → content encryption is now mandatory.")
    token = cfg["github_token"]
    if not token:
        raise GitHubError(0, "Enter the GitHub token first.")
    gh = GitHub(token)
    info = gh.check_token()
    owner = info["login"]
    progress(f"Token is valid (user: {owner}, type: {info['kind']}).")
    for w in info["warnings"]:
        progress("⚠ " + w)

    repo = None
    # 1) The repo stored in the config
    if cfg["repo_name"]:
        repo = gh.get_repo(cfg["repo_owner"] or owner, cfg["repo_name"])
        if repo:
            progress(f"Found the previous repo: {repo['full_name']}")
        else:
            progress("The stored repo no longer exists; searching again…")
    # 2) An empty/existing repo the user named
    if not repo and existing_repo:
        repo = gh.get_repo(owner, existing_repo)
        if not repo:
            raise GitHubError(404, f"Repo \"{existing_repo}\" was not found.")
        has_meta = gh.get_file(owner, existing_repo, P.META_FILE) is not None
        if not has_meta and not gh.repo_is_empty(owner, existing_repo):
            raise GitHubError(0, "This repo is not empty and does not belong to this project; refusing to avoid overwriting it.")
        progress(f"Using the existing repo: {repo['full_name']}")
    # 3) Search by .bridge-meta.json
    if not repo:
        progress("Searching the account for a previous project repo…")
        repo = gh.find_bridge_repo()
        if repo:
            progress(f"Found: {repo['full_name']} (rebuilding the local configuration)")
    # 4) Create a new repo
    if not repo:
        name = repo_name or f"{REPO_PREFIX}{secrets.token_hex(3)}"
        progress(f"Creating a {visibility} repo named {name}…")
        repo = gh.create_repo(name, private=(visibility == "private"))

    name = repo["name"]
    owner = repo["owner"]["login"]
    private = bool(repo.get("private"))
    visibility = "private" if private else "public"
    if not private:
        encrypt = True
    meta_raw = gh.get_file(owner, name, P.META_FILE)
    meta = P.loads(meta_raw, {}) if meta_raw else {}
    instance_id = meta.get("instance_id") or cfg["instance_id"] or secrets.token_hex(8)

    from local_server.config import ensure_content_key
    content_key = ensure_content_key()
    upload_secrets(gh, owner, name, passphrase=passphrase, content_key=content_key, progress=progress)

    push_code(token, owner, name, instance_id, progress)
    init_bus_branch(cfg, token, owner, name, progress)
    ensure_actions(gh, owner, name, progress)

    cfg.update({
        "repo_owner": owner, "repo_name": name, "instance_id": instance_id,
        "visibility": visibility, "default_branch": repo.get("default_branch") or "main",
        "encrypt": encrypt, "deployed_code_hash": code_fingerprint(),
    }, save=True)
    progress("Setup complete ✅")
    return {"repo": f"{owner}/{name}", "visibility": visibility, "encrypt": encrypt}
