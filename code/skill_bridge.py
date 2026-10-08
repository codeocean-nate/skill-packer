#!/usr/bin/env python3
"""Skill Bridge core: find your Aqua skills on a source Code Ocean deployment, read each one over Git,
build the prompt (or start the Skill Packer run) that copies them to a target deployment, and check the copies.

Used by app.py. Also a small CLI for headless checks:

    python skill_bridge.py list src|dst [--enrich]   ask Aqua for the enabled skills (about 30 s)
    python skill_bridge.py enrich SLUG [SLUG ...]     read skills from the source over Git
    python skill_bridge.py prompt SLUG [SLUG ...]     print the all-in-one prompt for the target's Aqua
    python skill_bridge.py run-packer SLUG [...]      run the Skill Packer on the target and wait
    python skill_bridge.py verify --computation ID --skills SLUG [...]
                                                      check the new skills on the target against the packer run
    python skill_bridge.py check                      check settings, tokens and the packer (no Aqua calls)

Configuration comes from the environment: SRC_HOST, SRC_CO_TOKEN, DST_HOST, DST_CO_TOKEN, PACKER_CAPSULE_ID.
Tokens are read from the environment only (verify asks for the target token if DST_CO_TOKEN is empty). They never
go on a command line, into a URL or into output.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import difflib
import getpass
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import requests
import yaml

# An internal CA gets installed into the system store, but requests trusts only certifi's bundle unless told.
SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"


def use_system_ca(env=None, bundle: str = SYSTEM_CA_BUNDLE) -> None:
    """Point requests (API calls and pre-signed downloads alike) at the system CA bundle, unless the environment
    already names a bundle."""
    env = os.environ if env is None else env
    if os.path.isfile(bundle) and not any(env.get(k) for k in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
                                                                "SSL_CERT_FILE")):
        env.setdefault("REQUESTS_CA_BUNDLE", bundle)


use_system_ca()

# Activating a skill returns its SKILL.md followed by "From capsule <uuid>, file SKILL.md." (custom skills only),
# so this wording makes Aqua report each custom skill's capsule. Asking for "a list of skills" instead makes Aqua
# search capsules, and capsule search doesn't return skill capsules.
LIST_PROMPT = (
    "Activate every skill in your available-skills list with your skills tool, one call per skill. "
    "For each, report the UUID of the capsule its SKILL.md was loaded from (or 'none' if it was not loaded "
    "from a capsule). Then call get_capsule on each such UUID and report the capsule's name and slug as a "
    "JSON array. Do not create, change or delete anything."
)
PACKER_PLACEHOLDER = "<PACKER_CAPSULE_ID>"
_COPY_RULES = (
    'For each skill in its "packed" list, create a separate stand-alone skill whose files are exact '
    'byte-for-byte copies of the files listed in that skill\'s "paths", keeping each file\'s path relative to '
    "its skill folder. Do not edit, reformat or improve anything. Commit each skill."
)
# Verified wording: Aqua skips packed skills whose names match custom skills the user already has (it checks
# its own available-skills list), so re-running the same selection creates nothing twice.
PACK_PROMPT = (
    "Run the Skill Packer capsule {packer} with skills = {slugs}. When it finishes, read report.json from the "
    "run's results. Before creating anything, compare each name in its \"packed\" list with the custom skills I "
    "already have, and skip any packed skill whose name matches one of mine. For each remaining skill, create a "
    "separate stand-alone skill whose files are exact byte-for-byte copies of the files listed in that skill's "
    "\"paths\", keeping each file's path relative to its skill folder. Do not edit, reformat or improve anything. "
    "Commit each skill. Then list each new skill's name and slug, the skills you skipped because I already have "
    "them, the computation ID, and anything the packer skipped with its reason."
)
UNPACK_PROMPT = (
    "Computation {computation} (a run of the Skill Packer capsule) has results under {bundle}/ in Claude plugin "
    "layout. Read report.json from the results. " + _COPY_RULES + " Report each new skill's name and slug."
)

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
SLUG_RE = re.compile(r"[0-9]{7}")                                # a slug is exactly 7 digits
CAPSULE_URL_RE = re.compile(r"/capsule[/-]([0-9]{7})(?![0-9])")  # …/capsule/1234567/tree, …/capsule-1234567.git
URL_RE = re.compile(r"https?://[^\s<>()\[\]\"'`|]+")
HOST_RE = re.compile(r"https://[A-Za-z0-9.-]+(:\d+)?")
DESC_MAX = 160
# Problems that mean the packer would skip the slug; such rows start unselected.
BLOCKING = ("clone failed", "not committed", "not a skill", "no SKILL.md", "frontmatter")


class BridgeError(Exception):
    """Something to show the user as one plain sentence."""


# ---------------------------------------------------------------------------------------------------------------
# Small helpers

def normalize_host(host: str) -> str:
    """'example.com' or 'https://example.com/' -> 'https://example.com'. Empty stays empty."""
    host = (host or "").strip().rstrip("/")
    if not host:
        return ""
    if "://" not in host:
        host = "https://" + host
    if not HOST_RE.fullmatch(host):
        raise BridgeError(f"Use a deployment URL like https://codeocean.example.com (got {host!r}).")
    return host


def slug_in(item: str) -> str | None:
    """The slug in one pasted item: in a URL or path, the 7 digits right after /capsule/; otherwise the item itself
    when it is exactly 7 digits (surrounding quotes, brackets and punctuation aside)."""
    item = str(item).strip()
    if "/" in item:
        m = CAPSULE_URL_RE.search(item)
        return m.group(1) if m else None
    item = item.strip("\"'`()[]{}<>*.,:;")
    return item if SLUG_RE.fullmatch(item) else None


def parse_slugs(text: str) -> list[str]:
    """Slugs or pasted /capsule/NNNNNNN URLs, separated by spaces, commas, semicolons or new lines."""
    out: list[str] = []
    for item in filter(None, re.split(r"[\s,;]+", text or "")):
        slug = slug_in(item)
        if slug and slug not in out:
            out.append(slug)
    return out


def slugs_on_host(text: str, host: str) -> tuple[list[str], list[str]]:
    """Like parse_slugs, but leaves out items with a URL on another deployment, because slugs differ between
    deployments. Returns (slugs, left-out items)."""
    want, keep, other = urlsplit(host or "").hostname, [], []
    for item in filter(None, re.split(r"[\s,;]+", text or "")):
        (other if any(urlsplit(u).hostname != want for u in URL_RE.findall(item)) else keep).append(item)
    return parse_slugs(" ".join(keep)), other


def build_pack_prompt(slugs: list[str], packer_id: str = "") -> str:
    return PACK_PROMPT.format(packer=packer_id.strip() or PACKER_PLACEHOLDER, slugs=", ".join(slugs))


def build_unpack_prompt(computation_id: str, bundle: str = "migrated-skills") -> str:
    return UNPACK_PROMPT.format(computation=computation_id, bundle=bundle or "migrated-skills")


def _short(text: str, limit: int = 300) -> str:
    text = " / ".join(line.strip() for line in str(text).splitlines() if line.strip())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------------------------------------------
# Code Ocean HTTP

class CoClient:
    """Code Ocean public API and Aqua API with one token (HTTP basic auth: token as user, empty password)."""

    def __init__(self, host: str, token: str, label: str = "source", timeout: float = 60):
        self.host = normalize_host(host)
        if not self.host:
            raise BridgeError(f"Set the {label} deployment URL.")
        if not token:
            raise BridgeError(f"No API token for the {label} deployment.")
        self.label, self.timeout = label, timeout
        self.session = requests.Session()
        self.session.auth = (token, "")
        self.session.headers["User-Agent"] = "skill-bridge"

    def request(self, method: str, path: str, **kw) -> requests.Response:
        kw.setdefault("timeout", self.timeout)
        kw.setdefault("allow_redirects", False)          # the token never follows a redirect
        try:
            r = self.session.request(method, self.host + path, **kw)
        except requests.exceptions.SSLError as e:
            raise BridgeError(f"Couldn't verify the TLS certificate of {self.host}. If it uses an internal CA, "
                              f"add that CA to this environment ({_short(e, 160)}).") from None
        except (requests.ConnectionError, requests.Timeout) as e:
            raise BridgeError(f"Couldn't reach {self.host} ({type(e).__name__}). Check the URL and that this "
                              "machine can reach that deployment.") from None
        if 300 <= r.status_code < 400:
            r.close()
            where = urlsplit(r.headers.get("Location") or "").netloc or "another address"
            raise BridgeError(f"The {self.label} deployment ({self.host}) redirected {method} {path} to {where} "
                              f"(HTTP {r.status_code}). Use the deployment's direct URL; the API token is not sent "
                              "on to a redirect.")
        if r.status_code >= 400:
            raise BridgeError(self._http_error(r, method, path))
        return r

    def _http_error(self, r: requests.Response, method: str, path: str) -> str:
        try:
            body = r.json()
            detail = body.get("message") or body.get("error") or json.dumps(body)
        except ValueError:
            detail = r.text
        detail = _short(detail, 200)
        if r.status_code == 401:
            return (f"The {self.label} deployment ({self.host}) rejected the API token (HTTP 401). Check that the "
                    "secret holds a current token for that deployment.")
        if r.status_code == 403:
            return (f"The {self.label} deployment ({self.host}) refused {method} {path} (HTTP 403: {detail}). "
                    "Check the token's scopes and your access.")
        if r.status_code == 404:
            return f"Not found on the {self.label} deployment ({self.host}): {method} {path} (HTTP 404)."
        return f"{method} {path} on the {self.label} deployment failed: HTTP {r.status_code} {detail}"

    # -- public API ---------------------------------------------------------------------------------------------
    def get_capsule(self, capsule_id: str) -> dict:
        return self.request("GET", f"/api/v1/capsules/{capsule_id}").json()

    def app_panel(self, capsule_id: str) -> list[dict]:
        return self.request("GET", f"/api/v1/capsules/{capsule_id}/app_panel").json().get("parameters") or []

    def owner_email(self) -> str:
        # Same lookup as the Skill Packer: everything a search for "ownership: private" returns belongs to the
        # token's owner, so the first owned capsule or data asset gives the owner's email (Git's username).
        for kind in ("capsules", "data_assets"):
            try:
                found = (self.request("POST", f"/api/v1/{kind}/search",
                                      json={"ownership": "private", "limit": 1}).json().get("results") or [{}])
            except BridgeError as e:
                if "HTTP 401" in str(e) or "Couldn't" in str(e):
                    raise
                continue
            if found and found[0].get("owner_email"):
                return found[0]["owner_email"]
        raise BridgeError(f"Couldn't work out the {self.label} token owner's email (you own no capsules or data "
                          f"assets there). Set the {self.label} Git username (your email) in Settings.")

    def start_computation(self, capsule_id: str, named_parameters: list[dict]) -> str:
        body = {"capsule_id": capsule_id, "named_parameters": named_parameters}
        return self.request("POST", "/api/v1/computations", json=body).json()["id"]

    def get_computation(self, computation_id: str) -> dict:
        return self.request("GET", f"/api/v1/computations/{computation_id}").json()

    def list_results(self, computation_id: str, path: str = "") -> list[dict]:
        """One folder of a run's results: [{"name", "path", "type": "file" | "folder", "size"}]."""
        return self.request("POST", f"/api/v1/computations/{computation_id}/results",
                            json={"path": path}).json().get("items") or []

    def result_bytes(self, computation_id: str, path: str) -> bytes | None:
        """A result file's bytes, or None if the run has no such file."""
        try:
            url = self.request("GET", f"/api/v1/computations/{computation_id}/results/download_url",
                               params={"path": path}).json().get("url")
        except BridgeError as e:
            if "HTTP 404" in str(e) or "HTTP 400" in str(e):
                return None
            raise
        if not url:
            return None
        try:
            r = requests.get(url, timeout=self.timeout)      # a pre-signed URL: no auth header
        except requests.RequestException as e:
            raise BridgeError(f"Couldn't download {path} from the results of {computation_id} at "
                              f"{urlsplit(url).netloc} ({type(e).__name__}). Check that this machine can reach it"
                              + (" and trusts its CA." if isinstance(e, requests.exceptions.SSLError) else ".")
                              ) from None
        return r.content if r.ok else None

    def result_text(self, computation_id: str, path: str) -> str | None:
        """A result file's text, or None if the run has no such file."""
        data = self.result_bytes(computation_id, path)
        return None if data is None else data.decode("utf-8", "replace")

    # -- Aqua ---------------------------------------------------------------------------------------------------
    def aqua(self, prompt: str, timeout: float = 600) -> "AquaResult":
        """One prompt in a new Aqua session. The same two endpoints the Aqua CLI uses."""
        t0 = time.time()
        try:
            sid = self.request("POST", "/api/aqua/sessions", json={}).json()["session_id"]
        except BridgeError as e:
            if "HTTP 404" in str(e):
                raise BridgeError(f"Aqua isn't available at {self.host} for this token (HTTP 404 on the Aqua "
                                  "sessions endpoint). Check that Aqua is enabled for your account there.") from None
            raise
        r = self.request("POST", f"/api/aqua/sessions/{sid}/invoke", json={"prompt": prompt}, stream=True,
                         timeout=(30, timeout))
        with r:
            reply, events = parse_sse(r.iter_lines(decode_unicode=True), host=self.host)
        return AquaResult(session_id=sid, reply=reply, events=events, seconds=round(time.time() - t0, 1))


@dataclass
class AquaResult:
    session_id: str
    reply: str
    events: list[dict]
    seconds: float = 0.0

    def tool_calls(self, name: str | None = None) -> list[dict]:
        return [e for e in self.events if e.get("type") == "tool_call" and (name is None or e.get("name") == name)]


def parse_sse(lines, host: str = "") -> tuple[str, list[dict]]:
    """Aqua's invoke stream -> (reply text, events). Event types: data (reply text), reasoning, tool_use,
    error, questions."""
    reply, events, kind = [], [], "message"
    for line in lines:
        if line is None:
            continue
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        if line.startswith("event:"):
            kind = line[6:].strip()
            continue
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        try:
            data = json.loads(raw)
        except ValueError:
            data = {"data": raw}
        if not isinstance(data, dict):
            data = {"data": data}
        if kind == "data":
            reply.append(str(data.get("data", "")))
        elif kind == "tool_use":
            events.append({"type": "tool_call", "name": data.get("name"), "input": data.get("input")})
        elif kind == "reasoning":
            if events and events[-1]["type"] == "thinking":
                events[-1]["text"] += str(data.get("data", ""))
            else:
                events.append({"type": "thinking", "text": str(data.get("data", ""))})
        elif kind == "error":
            raise BridgeError(f"Aqua on {host or 'the deployment'} returned an error: "
                              f"{_short(data.get('data') or data.get('message') or data, 300)}")
        elif kind == "questions":
            raise BridgeError("Aqua answered with a question instead of the list. Load the skills again.")
        else:
            events.append({"type": kind, "data": data})
    return "".join(reply), events


# ---------------------------------------------------------------------------------------------------------------
# Listing skills through Aqua

@dataclass
class Listing:
    skills: list[dict]                      # {"name", "capsule_name", "uuid", "slug"}
    available: list[str]                    # every skill name Aqua activated (built-in ones too)
    reply: str = ""
    seconds: float = 0.0
    session_id: str = ""
    notes: list[str] = field(default_factory=list)

    def names(self) -> set[str]:
        out = {s["name"].casefold() for s in self.skills if s.get("name")}
        out |= {s["capsule_name"].casefold() for s in self.skills if s.get("capsule_name")}
        return out | {n.casefold() for n in self.available}


def _slug_of(value) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10_000_000:
        return f"{value:07d}"                # a slug that Aqua wrote as a number loses its leading zeros
    if isinstance(value, str):
        return slug_in(value)
    return None


def _entry(d: dict) -> dict | None:
    low = {str(k).lower(): v for k, v in d.items()}
    uuid = None
    for key in ("uuid", "capsule_uuid", "capsule_id", "id", "source_capsule", "source_capsule_uuid"):
        v = low.get(key)
        if isinstance(v, str) and UUID_RE.fullmatch(v.strip()):
            uuid = v.strip().lower()
            break
    if not uuid:
        uuid = next((v.strip().lower() for v in low.values() if isinstance(v, str) and UUID_RE.fullmatch(v.strip())),
                    None)
    slug = None
    for key in ("slug", "capsule_slug", "link", "url"):
        slug = _slug_of(low.get(key))
        if slug:
            break
    skill = low.get("skill") or low.get("skill_name")
    cname = low.get("name") or low.get("capsule_name")
    if not (uuid or slug):
        return None
    name = str(skill or cname or "")
    return {"name": name, "capsule_name": str(cname or ""), "uuid": uuid, "slug": slug}


def _json_arrays(text: str):
    dec = json.JSONDecoder()
    for m in re.finditer(r"\[", text):
        try:
            obj, _ = dec.raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj):
            yield obj


def parse_listing(reply: str, events: list[dict]) -> tuple[list[dict], list[str]]:
    """Aqua's reply and tool calls -> (skill entries, every activated skill name)."""
    best: list[dict] = []
    for arr in _json_arrays(reply or ""):
        entries = [e for e in (_entry(d) for d in arr) if e]
        if len(entries) > len(best):
            best = entries
    available = []
    for e in events:
        if e.get("type") == "tool_call" and e.get("name") == "skills":
            n = (e.get("input") or {}).get("skill_name") if isinstance(e.get("input"), dict) else None
            if n and n not in available:
                available.append(n)
    if not best:
        # No JSON array: fall back to the capsules Aqua looked up (each still gets checked through the API).
        seen = []
        for e in events:
            if e.get("type") == "tool_call" and e.get("name") == "get_capsule" and isinstance(e.get("input"), dict):
                cid = str(e["input"].get("capsule_id", "")).strip().lower()
                if UUID_RE.fullmatch(cid) and cid not in seen:
                    seen.append(cid)
        best = [{"name": "", "capsule_name": "", "uuid": u, "slug": None} for u in seen]
    out, keys = [], set()
    for e in best:
        k = e["uuid"] or e["slug"]
        if k not in keys:
            keys.add(k)
            out.append(e)
    return out, available


def verify_entries(client: CoClient, entries: list[dict]) -> list[str]:
    """Check each entry's UUID through the public API and take the slug and name from there. Returns notes."""
    notes = []

    def one(e):
        if not e.get("uuid"):
            return None
        try:
            cap = client.get_capsule(e["uuid"])
        except BridgeError as err:
            return f"{e.get('name') or e['uuid']}: couldn't confirm through the API ({_short(err, 120)})"
        api_slug = str(cap.get("slug") or "")
        if api_slug and e.get("slug") and api_slug != e["slug"]:
            note = f"{e.get('name') or e['uuid']}: Aqua said slug {e['slug']}, the API says {api_slug}; using {api_slug}"
        else:
            note = None
        e["slug"] = api_slug or e.get("slug")
        e["capsule_name"] = cap.get("name") or e.get("capsule_name") or ""
        e["name"] = e.get("name") or e["capsule_name"]
        return note

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        notes = [n for n in pool.map(one, entries) if n]
    return notes


def list_skills(host: str, token: str, label: str = "source", verify: bool = True) -> Listing:
    """Ask Aqua on `host` for the enabled custom skills. About 30 seconds."""
    client = CoClient(host, token, label)
    res = client.aqua(LIST_PROMPT)
    entries, available = parse_listing(res.reply, res.events)
    if not entries:
        if not res.reply.strip():
            raise BridgeError(f"Aqua on {client.host} sent an empty reply. Load the skills again.")
        if not available:
            raise BridgeError(f"Aqua on {client.host} replied without a JSON list of skills. Load the skills again, "
                              "or paste the slugs from My Skills.")
    notes = verify_entries(client, entries) if verify else []
    skills = [e for e in entries if e.get("slug")]
    notes += [f"{e.get('name') or e.get('uuid')}: no slug found" for e in entries if not e.get("slug")]
    return Listing(skills=skills, available=available, reply=res.reply, seconds=res.seconds,
                   session_id=res.session_id, notes=notes)


# ---------------------------------------------------------------------------------------------------------------
# Reading skills over Git (same rules as the Skill Packer)

# git asks this helper for credentials. It answers only "get" for the deployment being cloned (SB_GIT_HOST, its
# host[:port]), so the token can't go to any other host; git is also told not to follow redirects.
_HELPER = ('!f() { test "$1" = get || exit 0; h=; '
           'while IFS= read -r l && [ -n "$l" ]; do case "$l" in host=*) h=${l#host=};; esac; done; '
           '[ "$h" = "$SB_GIT_HOST" ] || exit 0; '
           'printf "username=%s\\npassword=%s\\n" "$SB_GIT_USER" "$SB_GIT_TOKEN"; }; f')


def _git(args: list[str], env: dict, cwd: Path | None = None, timeout: int = 180, raw: bool = False,
         stdin: bytes | None = None):
    # The token reaches git through the environment and the credential helper, never argv or the URL.
    return subprocess.run(["git", "-c", "credential.helper=", "-c", "credential.helper=" + _HELPER,
                           "-c", "http.followRedirects=false", *args], cwd=cwd, env=env, check=True,
                          capture_output=True, text=not raw, input=stdin, timeout=timeout).stdout


def _git_env(git_user: str, token: str, host: str) -> dict:
    return dict(os.environ, SB_GIT_USER=git_user, SB_GIT_TOKEN=token, SB_GIT_HOST=urlsplit(host).netloc,
                GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="", SSH_ASKPASS="")


def _stderr(e: subprocess.SubprocessError) -> str:
    err = getattr(e, "stderr", None) or ""
    return err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err)   # bytes after a timeout


def _clone(url: str, dest: Path, env: dict, *options: str) -> str | None:
    """Shallow clone. Returns what went wrong, or None."""
    try:
        _git(["clone", "-q", "--depth", "1", "--no-tags", *options, url, str(dest)], env)
    except FileNotFoundError:
        raise BridgeError("git isn't installed in this environment. Add the git package.") from None
    except subprocess.TimeoutExpired as e:
        return "clone failed: timed out" + (f" ({_short(_stderr(e), 160)})" if _stderr(e).strip() else "")
    except subprocess.CalledProcessError as e:
        return "clone failed: " + (_short(_stderr(e), 220) or f"exit {e.returncode}")
    return None


def _hidden(path: Path, root: Path) -> bool:
    return any(part.startswith(".") for part in path.relative_to(root).parts)


def skill_dirs(repo: Path) -> list[Path]:
    top = repo / "SKILL.md"
    if top.is_file() or top.is_symlink():
        return [repo]                                                    # single skill
    # Hidden folders (.git, an .archive copy, ...) never hold the skill.
    found = sorted(p.parent for p in repo.rglob("SKILL.md")
                   if (p.is_file() or p.is_symlink()) and not _hidden(p, repo))
    return [d for d in found if not any(o in d.parents for o in found)]  # bundle children


def _first_line(e: BaseException) -> str:
    return (str(e).splitlines() or [type(e).__name__])[0]


def parse_frontmatter(text: str) -> tuple[dict | None, str | None]:
    """SKILL.md text -> (frontmatter, None) or (None, why it can't be used)."""
    try:
        m = re.match(r"---[ \t]*\r?\n(.*?)^---[ \t]*\r?$", text, re.S | re.M)
        fm = yaml.safe_load(m.group(1)) if m else None
    except (ValueError, yaml.YAMLError, RecursionError) as e:          # RecursionError: absurdly deep YAML
        return None, f"unreadable SKILL.md frontmatter: {_first_line(e)}"
    if not isinstance(fm, dict):
        return None, "SKILL.md has no YAML frontmatter between --- lines"
    if not isinstance(fm.get("name"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", fm["name"]):
        return None, f"SKILL.md frontmatter name is missing or not a valid folder name: {fm.get('name')!r}"
    return fm, None


def frontmatter(skill_dir: Path) -> tuple[dict | None, str | None]:
    path = skill_dir / "SKILL.md"
    if path.is_symlink():                                # never read through a link out of the clone
        return None, "not a skill (SKILL.md is a link)"
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, ValueError) as e:
        return None, f"unreadable SKILL.md frontmatter: {_first_line(e)}"
    return parse_frontmatter(text)


def tags_of(fm: dict) -> list[str]:
    tags = (fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}).get("tags")
    if isinstance(tags, str):
        tags = re.split(r"[,\s]+", tags)
    elif isinstance(tags, (int, float)) and not isinstance(tags, bool):
        tags = [tags]                                    # one unquoted number
    elif not isinstance(tags, (list, tuple, set)):
        tags = []                                        # a mapping, true/false or nothing: no tags
    out = []
    for t in tags:
        if isinstance(t, bool) or not isinstance(t, (str, int, float)):
            continue
        t = str(t).strip()
        if t and t.casefold() not in (x.casefold() for x in out):
            out.append(t)
    return out


def new_row(slug: str, origin: str = "aqua", uuid: str | None = None, capsule_name: str = "") -> dict:
    return {"slug": slug, "uuid": uuid, "capsule_name": capsule_name, "origin": origin, "names": [],
            "description": "", "tags": [], "commit": "", "files": 0, "bytes": 0, "problems": [], "notes": [],
            "on_target": []}


def read_skill_repo(repo: Path, row: dict) -> dict:
    """Fill `row` from a cloned skill repo (HEAD already checked)."""
    if (repo / ".codeocean").is_dir():                   # skill repos never have .codeocean/
        row["problems"].append("not a skill: a regular capsule (it has a .codeocean folder)")
        return row
    dirs = skill_dirs(repo)
    if not dirs:
        row["problems"].append("no SKILL.md in the repo")
        return row
    if len(dirs) > 1:
        row["notes"].append(f"bundle of {len(dirs)} skills")
    descs = []
    for d in dirs:
        fm, why = frontmatter(d)
        where = "/" + "/".join(d.relative_to(repo).parts)
        if why:
            row["problems"].append(f"{where}: {why}" if len(dirs) > 1 else why)
            continue
        row["names"].append(fm["name"])
        desc = fm.get("description")
        if isinstance(desc, str) and desc.strip():
            descs.append(desc.strip())
        for t in tags_of(fm):
            if t.casefold() not in (x.casefold() for x in row["tags"]):
                row["tags"].append(t)
        # Regular files only: links are never copied.
        files = [p for p in d.rglob("*") if p.is_file() and not p.is_symlink()
                 and ".git" not in p.relative_to(d).parts]
        row["files"] += len(files)
        row["bytes"] += sum(p.lstat().st_size for p in files)
    if not row["names"] and not row["problems"]:
        row["problems"].append("frontmatter problem in every SKILL.md")
    desc = " | ".join(descs)
    row["description"] = desc if len(desc) <= DESC_MAX else desc[: DESC_MAX - 1].rstrip() + "…"
    return row


def enrich(host: str, slug: str, git_user: str, token: str, origin: str = "aqua", uuid: str | None = None,
           capsule_name: str = "") -> dict:
    """Shallow-clone one skill from the source and read its SKILL.md frontmatter."""
    host = normalize_host(host)
    row = new_row(slug, origin, uuid, capsule_name)
    env = _git_env(git_user, token, host)
    with tempfile.TemporaryDirectory(prefix="skill-bridge-") as tmp:
        repo = Path(tmp) / "repo"
        problem = _clone(f"{host}/capsule-{slug}.git", repo, env)
        if problem:
            row["problems"].append(problem)
            return row
        try:
            row["commit"] = _git(["rev-parse", "--short", "HEAD"], env, cwd=repo).strip()
        except subprocess.CalledProcessError:
            row["problems"].append("not committed: commit the skill on the source")
            return row
        return read_skill_repo(repo, row)


def enrich_many(host: str, items: list[dict], git_user: str, token: str, workers: int = 6) -> list[dict]:
    """items: [{"slug", "origin"?, "uuid"?, "capsule_name"?}] -> rows in the same order."""
    def one(it):
        return enrich(host, it["slug"], git_user, token, it.get("origin", "aqua"), it.get("uuid"),
                      it.get("capsule_name", ""))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(one, items))


def is_blocked(row: dict) -> bool:
    return any(p.startswith(BLOCKING) or "frontmatter" in p or "not a skill" in p for p in row.get("problems", []))


def mark_on_target(rows: list[dict], target_names: set[str]) -> None:
    for row in rows:
        row["on_target"] = [n for n in row.get("names", []) if n.casefold() in target_names]


def row_status(row: dict) -> str:
    parts = list(row.get("problems", []))
    if row.get("on_target"):
        parts.append("already on target" + (f": {', '.join(row['on_target'])}" if len(row["names"]) > 1 else ""))
    parts += row.get("notes", [])
    if row.get("origin") == "manual":
        parts.append("added by slug")
    return "; ".join(parts) or "ready"


def default_selected(row: dict) -> bool:
    return not is_blocked(row) and not row.get("on_target")


# ---------------------------------------------------------------------------------------------------------------
# Running the Skill Packer on the target

def packer_parameters(panel: list[dict], slugs: list[str], source_host: str = "", git_user: str = "",
                      token_env: str = "") -> list[dict]:
    """Named parameters for a packer run. Only parameters the packer's App Panel declares are sent; the
    packer's own defaults apply to the rest."""
    names = {p.get("param_name"): p for p in panel}
    params = [{"param_name": "skills", "value": ", ".join(slugs)}]
    if source_host and "source_host" in names:
        params.append({"param_name": "source_host", "value": source_host})
    # A packer that looks up the email itself has an optional git_user; send it only where it's required.
    if git_user and names.get("git_user", {}).get("required"):
        params.append({"param_name": "git_user", "value": git_user})
    if token_env and "token_env" in names:
        params.append({"param_name": "token_env", "value": token_env})
    return params


def parse_packer_output(text: str) -> dict:
    """The packer prints {"packed": [...], "skipped": [...]} (plus a few plain lines) to its output."""
    text = text or ""
    dec = json.JSONDecoder()
    for m in re.finditer(r"^\{", text, re.M):
        try:
            obj, end = dec.raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(obj, dict) and ("packed" in obj or "skipped" in obj):
            rest = (text[: m.start()] + text[end:]).strip()
            return {"packed": obj.get("packed") or [], "skipped": obj.get("skipped") or [], "messages": rest}
    return {"packed": [], "skipped": [], "messages": text.strip()}


def run_packer(client: CoClient, packer_id: str, params: list[dict], timeout: float = 1200, poll: float = 5,
               on_update=None) -> dict:
    """Start the packer, wait for it, and read its output and report.json."""
    cid = client.start_computation(packer_id, params)
    if on_update:
        on_update({"id": cid, "state": "started"})
    t0, comp = time.time(), {}
    while time.time() - t0 < timeout:
        comp = client.get_computation(cid)
        if on_update:
            on_update(comp)
        if comp.get("state") in ("completed", "failed"):
            break
        time.sleep(poll)
    else:
        raise BridgeError(f"The packer run {cid} didn't finish within {int(timeout)} s. Check it in Code Ocean.")
    output = client.result_text(cid, "output") or ""
    report_text = client.result_text(cid, "report.json") if comp.get("has_results", True) else None
    try:
        report = json.loads(report_text) if report_text else None
    except ValueError:
        report = None
    parsed = parse_packer_output(output)
    if report:                                        # report.json has the slug, commit and files of each skill
        parsed["packed"] = report.get("packed") or parsed["packed"]
        parsed["skipped"] = report.get("skipped") or parsed["skipped"]
    # end_status can read "succeeded" for a run whose script exited non-zero, so exit_code decides.
    exit_code = comp.get("exit_code")
    ok = comp.get("state") == "completed" and exit_code == 0 and bool(parsed["packed"])
    return {"id": cid, "state": comp.get("state"), "end_status": comp.get("end_status"), "exit_code": exit_code,
            "run_time": comp.get("run_time"), "ok": ok, "output": output, "report": report,
            "bundle": (report or {}).get("bundle") or "migrated-skills", **parsed}


# ---------------------------------------------------------------------------------------------------------------
# Checking the copies on the target

STATUSES = ("OK", "MISMATCH", "MISSING", "EXTRA")
REGULAR = ("100644", "100755")                                    # Git modes of regular files
_SUMS_LINE = re.compile(r"([0-9a-fA-F]{64}) [ *](.+)")
_EXEC_NOTE = {"lost": "executable in the source, not in the copy",
              "maybe": "starts with #! but isn't executable in the copy"}


def parse_sums(text: str) -> dict[str, dict[str, str]]:
    """The packer's SHA256SUMS ("<sha256>  skills/<name>/<path>" lines) -> {name: {path: sha256}}. A line that
    starts with a backslash has an escaped path (\\\\, \\n, \\r), as GNU sha256sum writes it."""
    out: dict[str, dict[str, str]] = {}
    for line in (text or "").split("\n"):
        line = line.rstrip("\r")
        escaped = line.startswith("\\")
        m = _SUMS_LINE.fullmatch(line[1:] if escaped else line)
        path = m.group(2) if m else ""
        if escaped:
            path = re.sub(r"\\(.)", lambda e: {"n": "\n", "r": "\r", "\\": "\\"}.get(e.group(1), e.group(0)), path)
        parts = path.split("/", 2)
        if len(parts) == 3 and parts[0] == "skills" and parts[1] and parts[2]:
            out.setdefault(parts[1], {})[parts[2]] = m.group(1).lower()
    return out


def _in_skill(path: str, bundle: str, name: str) -> str:
    """A path from report.json ('<bundle>/skills/<name>/x', 'skills/<name>/x' or 'x') -> 'x'."""
    for prefix in (f"{bundle}/skills/{name}/", f"skills/{name}/"):
        if path.startswith(prefix):
            return path[len(prefix):]
    return path.lstrip("/")


def expected_skills(sums: dict[str, dict[str, str]], report: dict | None, bundle: str) -> dict[str, dict]:
    """Per packed skill: its checksums, the paths report.json lists and, when report.json has an "executables"
    list, the files that were executable in the source (None when it doesn't say)."""
    def blank(files=None):
        return {"sums": files or {}, "listed": set(), "executables": None, "slug": "", "commit": ""}
    out = {name: blank(files) for name, files in sums.items()}
    for p in (report or {}).get("packed") or []:
        if not (isinstance(p, dict) and p.get("name")):
            continue
        name = str(p["name"])
        e = out.setdefault(name, blank())
        e["slug"], e["commit"] = str(p.get("slug") or ""), str(p.get("commit") or "")
        e["listed"] = {_in_skill(x, bundle, name) for x in p.get("paths") or [] if isinstance(x, str)}
        if isinstance(p.get("executables"), list):
            e["executables"] = {_in_skill(x, bundle, name) for x in p["executables"] if isinstance(x, str)}
    return out


def _find_sums(client: CoClient, cid: str, hints: list[str]) -> tuple[str, str]:
    """(bundle folder, its SHA256SUMS text) from a packer run's results: the hinted folders first, then any
    top-level results folder that holds a SHA256SUMS file."""
    tried: list[str] = []
    for hint in hints:
        hint = (hint or "").strip().strip("/")
        if hint and hint not in tried:
            tried.append(hint)
            text = client.result_text(cid, f"{hint}/SHA256SUMS")
            if text is not None:
                return hint, text
    try:
        top = client.list_results(cid)
    except BridgeError as e:
        if "HTTP 404" not in str(e) and "HTTP 400" not in str(e):
            raise
        top = []
    for item in top:
        path = str(item.get("path") or "")
        if item.get("type") == "folder" and path and path not in tried and any(
                i.get("type") == "file" and i.get("name") == "SHA256SUMS" for i in client.list_results(cid, path)):
            text = client.result_text(cid, f"{path}/SHA256SUMS")
            if text is not None:
                return path, text
    raise BridgeError(f"The results of computation {cid} have no SHA256SUMS. Use the ID of a Skill Packer run that "
                      "packed at least one skill.")


def committed_files(repo: Path, env: dict) -> dict[str, dict]:
    """Every entry of the clone's HEAD commit -> {"mode", "data"}. Regular files are read from Git's objects, so
    no checkout or line-ending setting changes the bytes; links and submodules are never read (data None)."""
    entries = {}
    for rec in _git(["ls-tree", "-r", "-z", "HEAD"], env, cwd=repo, raw=True).split(b"\0"):
        if rec:
            meta, _, path = rec.partition(b"\t")
            mode, _kind, oid = meta.decode().split()
            entries[path.decode("utf-8", "replace")] = (mode, oid)
    oids = list(dict.fromkeys(oid for mode, oid in entries.values() if mode in REGULAR))
    blobs: dict[str, bytes] = {}
    if oids:
        out = _git(["cat-file", "--batch"], env, cwd=repo, raw=True, stdin="".join(o + "\n" for o in oids).encode())
        i = 0
        while i < len(out):                     # "<oid> blob <size>\n<bytes>\n" per object
            nl = out.index(b"\n", i)
            head = out[i:nl].split()
            if len(head) == 3:
                size = int(head[2])
                blobs[head[0].decode()] = out[nl + 1:nl + 1 + size]
                i = nl + 2 + size
            else:                               # "<oid> missing"
                i = nl + 1
    if len(blobs) != len(oids):
        raise ValueError(f"{len(oids) - len(blobs)} file(s) missing from the clone")
    return {p: {"mode": m, "data": blobs[o] if m in REGULAR else None} for p, (m, o) in entries.items()}


def first_difference(a: bytes, b: bytes) -> int:
    """Offset of the first differing byte (the shorter length if one is a prefix of the other)."""
    n, i = min(len(a), len(b)), 0
    while i < n and a[i:i + 4096] == b[i:i + 4096]:
        i += 4096
    while i < n and a[i] == b[i]:
        i += 1
    return min(i, n)


def _snippet(data: bytes, start: int, end: int) -> str:
    while end < len(data) and data[end] & 0xC0 == 0x80:          # end on a character boundary
        end += 1
    text = data[start:end].decode("utf-8", "backslashreplace")
    for ch, shown in (("\r", "\\r"), ("\n", "\\n"), ("\t", "\\t")):
        text = text.replace(ch, shown)
    return ("…" if start else "") + text + ("…" if end < len(data) else "")


def mismatch_detail(original: bytes | None, copy: bytes) -> str:
    """Where a copy first differs from the original, with a short snippet of each around that byte."""
    if original is None:
        return f"checksum differs ({len(copy)} bytes in the copy)"
    at = first_difference(original, copy)
    start = max(0, at - 24)
    while start and original[start] & 0xC0 == 0x80:              # start on a character boundary
        start -= 1
    detail = (f'first difference at byte {at}: original "{_snippet(original, start, at + 24)}", '
              f'copy "{_snippet(copy, start, at + 24)}"')
    if len(original) != len(copy):
        detail += f"; {len(original)} bytes in the original, {len(copy)} in the copy"
    return detail


def _skill_name(text: str) -> tuple[str, str | None]:
    """A SKILL.md's frontmatter name, or its first "name:" line when the frontmatter doesn't parse (so a damaged
    copy can still be matched and its first difference shown)."""
    fm, why = parse_frontmatter(text)
    if fm:
        return fm["name"], None
    m = re.search(r"^name:[ \t]*[\"']?([A-Za-z0-9][A-Za-z0-9._-]{0,63})", text, re.M)
    return (m.group(1) if m else ""), why


def _not_regular(mode: str) -> str:
    return {"120000": "a symbolic link", "160000": "a submodule"}.get(mode, f"not a regular file (mode {mode})")


def _verify_slug(host: str, slug: str, expected: dict[str, dict], env: dict, original) -> dict:
    """Clone one new skill from the target and compare its committed files with the packed skill of the same
    name. original(name, path, sha256) returns the packed file's bytes, to show where a copy differs."""
    res = {"slug": slug, "name": "", "matched": "", "root": "", "commit": "", "problems": [], "notes": [],
           "files": []}
    with tempfile.TemporaryDirectory(prefix="skill-bridge-") as tmp:
        repo = Path(tmp) / "repo"
        problem = _clone(f"{host}/capsule-{slug}.git", repo, env, "--no-checkout")
        if problem:
            res["problems"].append(problem)
            return res
        try:
            res["commit"] = _git(["rev-parse", "--short", "HEAD"], env, cwd=repo).strip()
        except subprocess.CalledProcessError:
            res["problems"].append("no commits: commit the new skill on the target")
            return res
        try:
            files = committed_files(repo, env)
        except (subprocess.SubprocessError, ValueError) as e:
            res["problems"].append(f"couldn't read the committed files ({_short(_stderr(e) or e, 160)})")
            return res
    root = ""
    if "SKILL.md" not in files:
        nested = [p[:-len("SKILL.md")] for p in files
                  if p.endswith("/SKILL.md") and not any(part.startswith(".") for part in p.split("/"))]
        if len(nested) != 1:
            res["problems"].append("no SKILL.md at the repo root")
            return res
        root = res["root"] = nested[0]
        res["problems"].append(f"the skill's files are in {root}, not at the repo root")
    top = files[root + "SKILL.md"]
    if top["mode"] not in REGULAR:
        res["problems"].append(f"SKILL.md is {_not_regular(top['mode'])}, not a file")
        return res
    name, why = _skill_name(top["data"].decode("utf-8-sig", "replace"))
    res["name"] = name
    if not name:
        res["problems"].append(f"couldn't read the skill name from SKILL.md ({why})")
        return res
    match = name if name in expected else next(iter(difflib.get_close_matches(name, list(expected), 1, 0.8)), "")
    if not match:
        res["problems"].append(f"its SKILL.md name {name!r} isn't one of the packed skills "
                               f"({', '.join(sorted(expected))})")
        return res
    if match != name:
        res["notes"].append(f"its SKILL.md name is {name!r}; compared with the packed skill {match!r}")
    res["matched"], exp, copy = match, expected[match], {}
    for path, f in files.items():
        if path.startswith(root):
            copy[path[len(root):]] = f
        else:
            res["files"].append({"path": path, "status": "EXTRA", "detail": "outside the skill's folder",
                                 "exec": None})
    for rel in sorted(set(exp["sums"]) | set(copy)):
        f, want = copy.get(rel), exp["sums"].get(rel)
        row = {"path": rel, "status": "OK", "detail": "", "exec": None}
        if f is None:
            row["status"], row["detail"] = "MISSING", "in the packed skill, not in the copy"
        elif f["mode"] not in REGULAR:                            # links are never read
            row["status"] = "MISMATCH" if want else "EXTRA"
            row["detail"] = _not_regular(f["mode"]) + (", not a file" if want else "")
        elif not want:
            row["status"] = "EXTRA"
            row["detail"] = ("SHA256SUMS has no checksum for it (a link in the source)" if rel in exp["listed"]
                             else "not in the packed skill")
        elif hashlib.sha256(f["data"]).hexdigest() != want:
            row["status"], row["detail"] = "MISMATCH", mismatch_detail(original(match, rel, want), f["data"])
        # The packer's checksums don't record file modes: report.json's "executables" does, when it is there.
        if want and f is not None and f["mode"] == "100644":
            if exp["executables"] is not None:
                row["exec"] = "lost" if rel in exp["executables"] else None
            elif f["data"].startswith(b"#!"):
                row["exec"] = "maybe"
        res["files"].append(row)
    return res


def _verify_inputs(computation_id: str, new_slugs: list[str]) -> tuple[str, list[str]]:
    m = UUID_RE.search(computation_id or "")
    if not m:
        raise BridgeError("Enter the computation ID of the Skill Packer run (a UUID such as "
                          "0a1b2c3d-0000-4000-8000-000000000000).")
    slugs = parse_slugs(" ".join(new_slugs or []))
    if not slugs:
        raise BridgeError("Enter the slugs or URLs of the new skills on the target.")
    return m.group().lower(), slugs


def verify_copies(client_dst: CoClient, computation_id: str, new_slugs: list[str], git_user: str | None, token: str,
                  bundle_hint: str | None = None) -> dict:
    """Check the skills Aqua created on the target against the Skill Packer run they were copied from: each file's
    SHA-256 against the run's SHA256SUMS, files missing from or extra in each copy, and executable bits. Reads the
    run's results through the API and each new skill over Git (git_user defaults to the target token's owner).
    Changes nothing."""
    cid, slugs = _verify_inputs(computation_id, new_slugs)
    try:
        state = client_dst.get_computation(cid).get("state")
    except BridgeError as e:
        if "HTTP 404" in str(e):
            raise BridgeError(f"Computation {cid} wasn't found on {client_dst.host}. Use the ID of the Skill Packer "
                              "run on this deployment.") from None
        raise
    if state not in ("completed", "failed"):
        raise BridgeError(f"Computation {cid} hasn't finished (state: {state}). Check again when it has.")
    try:
        report = json.loads(client_dst.result_text(cid, "report.json") or "null")
    except ValueError:
        report = None
    report = report if isinstance(report, dict) else None
    bundle, sums = _find_sums(client_dst, cid, [bundle_hint or "", str((report or {}).get("bundle") or ""),
                                                "migrated-skills"])
    expected = expected_skills(parse_sums(sums), report, bundle)
    if not expected:
        raise BridgeError(f"{bundle}/SHA256SUMS in computation {cid} lists no skill files.")
    # A pasted reply can include the source slugs too; those aren't the copies.
    source = {e["slug"]: n for n, e in expected.items() if e["slug"]}
    notes = [f"Left out {s}: it is the source slug of {source[s]}. Use the new skill's slug on the target."
             for s in slugs if s in source]
    slugs = [s for s in slugs if s not in source]
    if not slugs:
        raise BridgeError(" ".join(notes))
    env = _git_env(git_user or client_dst.owner_email(), token, client_dst.host)

    def original(name: str, rel: str, sha: str) -> bytes | None:
        try:
            data = client_dst.result_bytes(cid, f"{bundle}/skills/{name}/{rel}")
        except BridgeError:
            return None
        return data if data is not None and hashlib.sha256(data).hexdigest() == sha else None

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(slugs))) as pool:
        skills = list(pool.map(lambda s: _verify_slug(client_dst.host, s, expected, env, original), slugs))
    counts = dict.fromkeys(STATUSES, 0)
    for f in (f for s in skills for f in s["files"]):
        counts[f["status"]] += 1
    matched = {s["matched"] for s in skills}
    res = {"computation": cid, "host": client_dst.host, "bundle": bundle,
           "source": str((report or {}).get("source") or ""),
           "packed": [{"name": n, "slug": e["slug"], "commit": e["commit"]} for n, e in expected.items()],
           "executables_listed": any(e["executables"] is not None for e in expected.values()),
           "skills": skills, "notes": notes, "counts": counts,
           "not_found": [{"name": n, "slug": e["slug"]} for n, e in expected.items() if n not in matched],
           "exec": [{"slug": s["slug"], "path": f["path"], "repo_path": s["root"] + f["path"],
                     "certain": f["exec"] == "lost"} for s in skills for f in s["files"] if f["exec"]]}
    bad = counts["MISMATCH"] + counts["MISSING"] + counts["EXTRA"] + sum(bool(s["problems"]) for s in skills)
    res["ok"] = not bad and not res["not_found"]
    return res


def _n(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _file_detail(f: dict) -> str:
    return "; ".join(x for x in (f["detail"], _EXEC_NOTE.get(f["exec"] or "", "")) if x)


def verify_rows(res: dict) -> list[dict]:
    """The check as table rows: notes, then each new skill's problems and files, then packed skills not checked."""
    rows = [{"Slug": "", "Skill": "", "File": "", "Result": "NOTE", "Details": n} for n in res["notes"]]
    for s in res["skills"]:
        base = {"Slug": s["slug"], "Skill": s["matched"] or s["name"]}
        rows += [{**base, "File": "", "Result": "NOTE", "Details": n} for n in s["notes"]]
        rows += [{**base, "File": "", "Result": "PROBLEM", "Details": p} for p in s["problems"]]
        rows += [{**base, "File": f["path"], "Result": f["status"], "Details": _file_detail(f)} for f in s["files"]]
    rows += [{"Slug": "", "Skill": n["name"], "File": "", "Result": "NOT CHECKED",
              "Details": "not found among the slugs you gave" + (f" (source slug {n['slug']})" if n["slug"] else "")}
             for n in res["not_found"]]
    return rows


def verify_verdict(res: dict) -> tuple[str, str]:
    """("success" | "warning" | "error", the overall result in plain words)."""
    c = res["counts"]
    stuck = sum(1 for s in res["skills"] if s["problems"])
    bad = [x for x in (c["MISMATCH"] and _n(c["MISMATCH"], "file differs", "files differ"),
                       c["MISSING"] and _n(c["MISSING"], "file is missing", "files are missing"),
                       c["EXTRA"] and _n(c["EXTRA"], "extra file", "extra files"),
                       stuck and _n(stuck, "skill has a problem", "skills have problems")) if x]
    unchecked = ", ".join(n["name"] for n in res["not_found"])
    if bad:
        return "error", ("The copies don't match the packed originals: " + ", ".join(bad) + ". Fix the rows marked "
                         "MISMATCH, MISSING, EXTRA or PROBLEM in the new skills, then check again."
                         + (f" Not checked yet: {unchecked}." if unchecked else ""))
    if unchecked:
        return "warning", (f"{_n(c['OK'], 'checked file matches', 'checked files match')} the packed originals, but "
                           f"these packed skills weren't checked: {unchecked}. Add their new slugs and check again.")
    return "success", (f"Every file matches its packed original ({_n(c['OK'], 'file', 'files')} in "
                       f"{_n(len(res['skills']), 'skill', 'skills')}).")


def verify_exec_note(res: dict) -> str:
    """What to do about files that aren't executable in a copy, or "" if there are none."""
    sure = [f"{i['path']} ({i['slug']})" for i in res["exec"] if i["certain"]]
    maybe = [f"{i['path']} ({i['slug']})" for i in res["exec"] if not i["certain"]]
    parts = []
    if sure:
        parts.append(f"{_n(len(sure), 'file was', 'files were')} executable in the source but "
                     f"{'is' if len(sure) == 1 else 'are'} not in the copy: {', '.join(sure)}.")
    if maybe:
        one = len(maybe) == 1
        parts.append(f"{_n(len(maybe), 'file starts', 'files start')} with #! but {'is' if one else 'are'} not "
                     f"executable in the copy: {', '.join(maybe)}. If {'it was' if one else 'they were'} executable "
                     f"in the source, make {'it' if one else 'them'} executable again.")
    if parts:
        parts.append("In a clone of the new skill, run git update-index --chmod=+x <file>, then commit and push:")
    return " ".join(parts)


def exec_fix_commands(res: dict) -> str:
    """Shell commands that set the executable bit again, one clone per new skill."""
    paths: dict[str, list[str]] = {}
    for i in res["exec"]:
        paths.setdefault(i["slug"], []).append(i["repo_path"])
    lines = []
    for slug, files in paths.items():
        lines += [f"git clone {res['host']}/capsule-{slug}.git && cd capsule-{slug}",
                  "git update-index --chmod=+x " + " ".join(shlex.quote(p) for p in files),
                  'git commit -m "Make scripts executable" && git push && cd ..']
    return "\n".join(lines)


def format_verify(res: dict) -> list[str]:
    """The check as plain text: one line per file, then a summary."""
    lines = [f"Computation {res['computation']} on {res['host']}: "
             f"{_n(len(res['packed']), 'packed skill', 'packed skills')} in {res['bundle']}/"]
    lines += [f"note: {n}" for n in res["notes"]]
    for s in res["skills"]:
        lines.append(f"{s['slug']}  {s['matched'] or s['name'] or '(name unknown)'}"
                     + (f"  (commit {s['commit']})" if s["commit"] else ""))
        lines += [f"  {'NOTE':<11} {n}" for n in s["notes"]]
        lines += [f"  {'PROBLEM':<11} {p}" for p in s["problems"]]
        for f in s["files"]:
            detail = _file_detail(f)
            lines.append(f"  {f['status']:<11} {f['path']}" + (f"  ({detail})" if detail else ""))
    for n in res["not_found"]:
        lines.append(f"NOT CHECKED  {n['name']}" + (f" (source slug {n['slug']})" if n["slug"] else "")
                     + ": not found among the slugs you gave")
    c = res["counts"]
    lines += ["", "Summary: " + ", ".join(f"{c[k]} {k}" for k in STATUSES)
              + f" in {_n(len(res['skills']), 'skill', 'skills')}.", verify_verdict(res)[1]]
    note = verify_exec_note(res)
    if note:
        lines += ["", "Note: " + note] + ["  " + cmd for cmd in exec_fix_commands(res).splitlines()]
    return lines


# ---------------------------------------------------------------------------------------------------------------
# Config and CLI

@dataclass
class Config:
    src_host: str = ""
    dst_host: str = ""
    src_token_env: str = "SRC_CO_TOKEN"
    dst_token_env: str = "DST_CO_TOKEN"
    packer_id: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        return cls(src_host=os.environ.get("SRC_HOST", ""), dst_host=os.environ.get("DST_HOST", ""),
                   packer_id=os.environ.get("PACKER_CAPSULE_ID", ""))

    @staticmethod
    def token(env_name: str) -> str:
        return (os.environ.get(env_name.strip(), "") if env_name and env_name.strip() else "").strip()


def check(cfg: Config) -> tuple[bool, list[str]]:
    """Settings and connection check for the reproducible run: no Aqua calls, nothing changes.
    Returns (all required checks passed, report lines). Token values never appear in the report."""
    lines, ok = [], True

    def line(passed: bool | None, what: str, detail: str):
        lines.append(f"{'OK  ' if passed else ('-   ' if passed is None else 'FAIL')} {what}: {detail}")

    try:
        line(True, "git", subprocess.run(["git", "--version"], capture_output=True, text=True,
                                         timeout=30).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        ok = False
        line(False, "git", "not installed; add the git package to the environment")
    sides = (("source", cfg.src_host, cfg.src_token_env, True), ("target", cfg.dst_host, cfg.dst_token_env, False))
    clients = {}
    for label, host, env_name, required in sides:
        token = Config.token(env_name)
        try:
            host = normalize_host(host)
        except BridgeError as e:
            ok = False
            line(False, f"{label} URL", str(e))
            continue
        if not (host and token):
            missing = " and ".join(x for x, v in (("URL", host), (f"token ({env_name})", token)) if not v)
            ok = ok and not required
            line(False if required else None, label, f"{missing} not set" + ("" if required else " (optional)"))
            continue
        try:
            client = CoClient(host, token, label)
            client.owner_email()
            clients[label] = client
            line(True, label, f"{host} accepted the token in {env_name}")
        except BridgeError as e:
            ok = ok and not required
            line(False, label, str(e))
    if cfg.packer_id and "target" in clients:
        try:
            panel = clients["target"].app_panel(cfg.packer_id)
            line(True, "Skill Packer", f"{cfg.packer_id} has parameters "
                 + ", ".join(p.get("param_name", "?") + ("*" if p.get("required") else "") for p in panel))
        except BridgeError as e:
            line(False, "Skill Packer", str(e))
    elif cfg.packer_id:
        line(None, "Skill Packer", "set, but there's no target token to check it with")
    else:
        line(None, "Skill Packer", "PACKER_CAPSULE_ID not set (optional; the prompt gets a placeholder)")
    return ok, lines


def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Skill Bridge headless checks (tokens come from the environment).")
    ap.add_argument("--src-token-env", default="SRC_CO_TOKEN")
    ap.add_argument("--dst-token-env", default="DST_CO_TOKEN")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="check settings, tokens and the Skill Packer capsule (no Aqua calls)")
    p = sub.add_parser("list")
    p.add_argument("side", choices=["src", "dst"])
    p.add_argument("--enrich", action="store_true", help="also read each skill over Git (source only)")
    p = sub.add_parser("enrich")
    p.add_argument("slugs", nargs="+")
    p = sub.add_parser("prompt")
    p.add_argument("slugs", nargs="+")
    p = sub.add_parser("run-packer")
    p.add_argument("slugs", nargs="+")
    p.add_argument("--token-env-param", default="", help="value for the packer's token_env parameter")
    p = sub.add_parser("verify", help="check the new skills on the target against the packer run's checksums")
    p.add_argument("--computation", required=True, help="the Skill Packer run's computation ID")
    p.add_argument("--skills", nargs="+", required=True, help="the new skills' slugs or URLs on the target")
    p.add_argument("--dst-host", default="", help="target deployment URL (default: DST_HOST)")
    p.add_argument("--git-user", default="", help="your email on the target (default: the token owner's)")
    p.add_argument("--bundle", default="", help="bundle folder in the run's results (default: from report.json)")
    a = ap.parse_args(argv)
    cfg = Config.from_env()
    cfg.src_token_env, cfg.dst_token_env = a.src_token_env, a.dst_token_env
    src_tok, dst_tok = Config.token(a.src_token_env), Config.token(a.dst_token_env)
    try:
        if a.cmd == "check":
            passed, lines = check(cfg)
            print("\n".join(lines))
            print("All required checks passed." if passed else "Some required checks failed.")
            return 0 if passed else 1
        if a.cmd == "list":
            host, tok = (cfg.src_host, src_tok) if a.side == "src" else (cfg.dst_host, dst_tok)
            lst = list_skills(host, tok, "source" if a.side == "src" else "target")
            out = {"seconds": lst.seconds, "session_id": lst.session_id, "skills": lst.skills,
                   "available": lst.available, "notes": lst.notes}
            if a.enrich and a.side == "src":
                user = CoClient(cfg.src_host, src_tok).owner_email()
                out["rows"] = enrich_many(cfg.src_host, [{"slug": s["slug"], "uuid": s["uuid"],
                                                          "capsule_name": s["capsule_name"]} for s in lst.skills],
                                          user, src_tok)
            print(json.dumps(out, indent=2))
        elif a.cmd == "enrich":
            user = CoClient(cfg.src_host, src_tok).owner_email()
            print(json.dumps(enrich_many(cfg.src_host, [{"slug": s, "origin": "manual"}
                                                        for s in parse_slugs(" ".join(a.slugs))], user, src_tok),
                             indent=2))
        elif a.cmd == "prompt":
            print(build_pack_prompt(parse_slugs(" ".join(a.slugs)), cfg.packer_id))
        elif a.cmd == "run-packer":
            if not cfg.packer_id:
                raise BridgeError("Set PACKER_CAPSULE_ID.")
            dst = CoClient(cfg.dst_host, dst_tok, "target")
            panel = dst.app_panel(cfg.packer_id)
            git_user = CoClient(cfg.src_host, src_tok).owner_email() if src_tok else ""
            params = packer_parameters(panel, parse_slugs(" ".join(a.slugs)), normalize_host(cfg.src_host),
                                       git_user, a.token_env_param)
            print("parameters:", json.dumps([p["param_name"] for p in params]), file=sys.stderr)
            res = run_packer(dst, cfg.packer_id, params,
                             on_update=lambda c: print("state:", c.get("state"), file=sys.stderr))
            res.pop("report", None)
            print(json.dumps(res, indent=2))
            print("\n" + build_unpack_prompt(res["id"], res["bundle"]))
        elif a.cmd == "verify":
            host = normalize_host(a.dst_host or cfg.dst_host)
            if not host:
                raise BridgeError("Set the target deployment URL in DST_HOST or with --dst-host.")
            slugs, elsewhere = slugs_on_host(" ".join(a.skills), host)
            for item in elsewhere:
                print(f"left out {item}: it is not on the target {host}", file=sys.stderr)
            _verify_inputs(a.computation, slugs)                 # before asking for the token
            tok = dst_tok
            if not tok:                                          # typed in, never echoed or stored
                try:
                    tok = getpass.getpass(f"API token for {host} (not shown): ").strip()
                except (EOFError, KeyboardInterrupt):
                    print(file=sys.stderr)
            res = verify_copies(CoClient(host, tok, "target"), a.computation, slugs, a.git_user, tok,
                                a.bundle or None)
            print("\n".join(format_verify(res)))
            return 0 if res["ok"] else 1
    except BridgeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
