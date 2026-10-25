"""GitHub REST client for the local server (create repos, Secrets, control Actions) with clear English error messages."""
from __future__ import annotations

import base64
import email.utils
import re
import time

import requests

from common import protocol as P

API = "https://api.github.com"
ACTIVE = {"queued", "in_progress", "waiting", "requested", "pending"}
REPO_DESCRIPTION = "Workspace notes and configuration sync"
REPO_PREFIX = "workspace-notes-"
_LEGACY_PREFIXES = ("personal-archive-", "workspace-notes-", "notes-sync-")


class GitHubError(Exception):
    def __init__(self, status: int, message: str, raw: str = ""):
        super().__init__(message)
        self.status = status
        self.message = message
        self.raw = raw


def token_kind(token: str) -> str:
    if token.startswith("github_pat_"):
        return "fine"
    if token.startswith("ghp_"):
        return "classic"
    return "unknown"


class GitHub:
    def __init__(self, token: str, timeout: int = 30):
        self.token = token.strip()
        self.kind = token_kind(self.token)
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "python-requests/2.32.3",
        })

    # -- Basics --------------------------------------------------------------------
    def _req(self, method: str, path: str, ok=(200,), **kw) -> requests.Response:
        try:
            r = self.s.request(method, API + path, timeout=self.timeout, **kw)
        except requests.RequestException as e:
            raise GitHubError(0, "Could not connect to api.github.com. (This is only needed for setup/control; "
                                 "message and file exchange continues over git.)", str(e)) from e
        date = r.headers.get("Date")
        if date:
            try:
                P.set_clock_offset(email.utils.parsedate_to_datetime(date).timestamp() - time.time())
            except (TypeError, ValueError):
                pass
        if r.status_code not in ok:
            raise self._error(r)
        return r

    def _error(self, r: requests.Response) -> GitHubError:
        try:
            msg = (r.json() or {}).get("message", "")
        except ValueError:
            raw_txt = r.text or ""
            m = re.search(r"<p><strong>(.*?)</strong></p>", raw_txt, re.I | re.S)
            if not m:
                m = re.search(r"<title>(.*?)</title>", raw_txt, re.I | re.S)
            msg = m.group(1).strip() if m else raw_txt[:200]
        s = r.status_code
        low = msg.lower()
        if s == 401:
            text = "The token is invalid, expired or revoked."
        elif s == 403 and "rate limit" in low:
            text = "GitHub's API rate limit has been reached; try again in a little while."
        elif s == 403:
            text = ("The token does not have enough access for this action. " + self.scope_hint() + f" (GitHub message: {msg})")
        elif s == 404:
            text = ("Not found, or the token has no access to it (GitHub returns 404 to hide private repos). "
                    + self.scope_hint())
        elif s == 422 and "name already exists" in low:
            text = "A repo with this name already exists in the account."
        elif s == 422:
            text = f"GitHub rejected the request: {msg}"
        else:
            text = f"GitHub error (HTTP {s}): {msg}"
        return GitHubError(s, text, r.text[:500])

    def scope_hint(self) -> str:
        if self.kind == "classic":
            return "A Classic token needs the repo and workflow scopes."
        if self.kind == "fine":
            return ("A Fine-grained token needs: Contents, Actions, Secrets and Workflows (all four Read & write) "
                    "and Metadata (Read), plus access to this repo. (Workflows is required because the app pushes "
                    "files under .github/workflows.)")
        return "Unknown token type; use a Classic or Fine-grained token."

    # -- Token ---------------------------------------------------------------------
    def check_token(self) -> dict:
        r = self._req("GET", "/user")
        login = r.json()["login"]
        warnings: list[str] = []
        if self.kind == "classic":
            scopes = {s.strip() for s in r.headers.get("X-OAuth-Scopes", "").split(",") if s.strip()}
            missing = [s for s in ("repo", "workflow") if s not in scopes]
            if missing:
                raise GitHubError(403, "The Classic token is missing these scopes: " + ", ".join(missing)
                                  + ". Create a new token with the repo and workflow scopes.")
        elif self.kind == "fine":
            warnings.append(
                "This is a Fine-grained token: Contents, Actions, Secrets and Workflows must be Read & write on the repo. "
                "For the app to create a new repo itself, the token also needs account-level \"Administration: Read & write\"; "
                "otherwise create an empty repo yourself and enter its name in the wizard.")
        else:
            warnings.append("Unknown token type; if you get access errors in later steps, create a Classic token.")
        return {"login": login, "kind": self.kind, "warnings": warnings}

    # -- Repo ----------------------------------------------------------------------
    def get_repo(self, owner: str, name: str) -> dict | None:
        try:
            return self._req("GET", f"/repos/{owner}/{name}").json()
        except GitHubError as e:
            if e.status == 404:
                return None
            raise

    def get_file(self, owner: str, name: str, path: str) -> bytes | None:
        try:
            r = self._req("GET", f"/repos/{owner}/{name}/contents/{path}",
                          headers={"Accept": "application/vnd.github.raw+json"})
            return r.content
        except GitHubError as e:
            if e.status == 404:
                return None
            raise

    def repo_is_empty(self, owner: str, name: str) -> bool:
        try:
            self._req("GET", f"/repos/{owner}/{name}/contents/")
            return False
        except GitHubError as e:
            if e.status == 404:
                return True
            raise

    def find_bridge_repo(self) -> dict | None:
        """Finds the project repo by .bridge-meta.json (after installing on a new device)."""
        repos: list[dict] = []
        page = 1
        while page <= 5:
            r = self._req("GET", "/user/repos", params={"per_page": 100, "page": page, "affiliation": "owner",
                                                        "sort": "pushed"})
            batch = r.json()
            repos += batch
            if len(batch) < 100:
                break
            page += 1
        cands = [
            x for x in repos
            if any(x["name"].startswith(p) for p in _LEGACY_PREFIXES)
            or x.get("description") in (REPO_DESCRIPTION, "Personal notes sync archive")
        ]
        for repo in cands:
            raw = self.get_file(repo["owner"]["login"], repo["name"], P.META_FILE)
            meta = P.loads(raw, {}) if raw else {}
            if meta.get("instance_id") or meta.get("project") == "tg-bridge":
                repo["_meta"] = meta
                return repo
        return None

    def create_repo(self, name: str, private: bool) -> dict:
        try:
            return self._req("POST", "/user/repos", ok=(201,), json={
                "name": name, "description": REPO_DESCRIPTION, "private": private, "auto_init": False,
                "has_issues": False, "has_wiki": False, "has_projects": False,
            }).json()
        except GitHubError as e:
            if e.status in (403, 404) and self.kind == "fine":
                raise GitHubError(e.status, "This token cannot create a new repo; either add the Administration permission "
                                            "or create an empty repo beforehand and enter its name in the settings.", e.raw) from e
            raise

    # -- Secrets -------------------------------------------------------------------------
    def put_secret(self, owner: str, name: str, secret_name: str, value: str) -> None:
        try:
            from nacl import encoding, public
        except ImportError as e:
            raise GitHubError(0, "The PyNaCl package is not installed (required to encrypt Secrets). "
                                 "On Termux: pkg install libsodium && pip install pynacl") from e
        k = self._req("GET", f"/repos/{owner}/{name}/actions/secrets/public-key").json()
        box = public.SealedBox(public.PublicKey(k["key"].encode("utf-8"), encoding.Base64Encoder()))
        enc = base64.b64encode(box.encrypt(value.encode("utf-8"))).decode("ascii")
        self._req("PUT", f"/repos/{owner}/{name}/actions/secrets/{secret_name}", ok=(201, 204),
                  json={"encrypted_value": enc, "key_id": k["key_id"]})

    # -- Actions -------------------------------------------------------------------------
    def actions_enabled(self, owner: str, name: str) -> bool:
        return bool(self._req("GET", f"/repos/{owner}/{name}/actions/permissions").json().get("enabled"))

    def enable_actions(self, owner: str, name: str) -> None:
        self._req("PUT", f"/repos/{owner}/{name}/actions/permissions", ok=(204,),
                  json={"enabled": True, "allowed_actions": "all"})

    def list_workflows(self, owner: str, name: str) -> list[dict]:
        return self._req("GET", f"/repos/{owner}/{name}/actions/workflows").json().get("workflows", [])

    def enable_workflow(self, owner: str, name: str, workflow_id: int) -> None:
        self._req("PUT", f"/repos/{owner}/{name}/actions/workflows/{workflow_id}/enable", ok=(204,))

    def dispatch(self, owner: str, name: str, ref: str = "main", workflow: str = P.WORKFLOW_FILE) -> None:
        self._req("POST", f"/repos/{owner}/{name}/actions/workflows/{workflow}/dispatches", ok=(204,),
                  json={"ref": ref})

    def active_runs(self, owner: str, name: str, workflow: str = P.WORKFLOW_FILE) -> list[dict]:
        r = self._req("GET", f"/repos/{owner}/{name}/actions/workflows/{workflow}/runs", params={"per_page": 10})
        return [x for x in r.json().get("workflow_runs", []) if x.get("status") in ACTIVE]

    def cancel_run(self, owner: str, name: str, run_id: int | str) -> None:
        self._req("POST", f"/repos/{owner}/{name}/actions/runs/{run_id}/cancel", ok=(202,))
