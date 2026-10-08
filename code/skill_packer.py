#!/usr/bin/env python3
# Skill Packer: clone skills from a source Code Ocean deployment and write them
# to /results in Claude plugin layout, ready for Aqua to unpack into skills.
import argparse, atexit, base64, datetime, hashlib, json, os, pathlib, re, shutil, ssl, stat, subprocess, sys
import tempfile, urllib.request
from urllib.parse import urlsplit
import yaml

VERSION = "2026.10.08"
SYSTEM_CA = "/etc/ssl/certs/ca-certificates.crt"   # update-ca-certificates writes every trusted CA here
RESERVED_BUNDLES = {"output"}                       # Code Ocean writes the run log to /results/output

# App Panel named parameters arrive as --name value, so an empty optional field can't shift the rest.
ap = argparse.ArgumentParser(description="Pack Code Ocean skills into a Claude plugin layout.")
ap.add_argument("--skills", required=True, help="slugs or /capsule/NNNNNNN URLs")
ap.add_argument("--git_user", default="", help="the token owner's email; looked up from the token when empty")
ap.add_argument("--keyword", default="", help="optional tag filter")
ap.add_argument("--bundle", default="migrated-skills", help="output folder and plugin name")
ap.add_argument("--source_host", default="", help="source deployment URL; defaults to the SRC_HOST env var")
ap.add_argument("--token_env", default="", help="env var holding the source API token; defaults to SRC_CO_TOKEN")
args = ap.parse_args()
print(f"Skill Packer {VERSION}")
skills_arg, git_user, keyword, bundle = (args.skills.strip(), args.git_user.strip(),
                                         args.keyword.strip(), args.bundle.strip() or "migrated-skills")
SRC = (args.source_host.strip() or os.environ.get("SRC_HOST", "")).rstrip("/")
if not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d+)?", SRC):
    sys.exit("Set the source deployment as https://<host>, in the source_host parameter or the SRC_HOST env var.")
token_env = args.token_env.strip() or "SRC_CO_TOKEN"
if not os.environ.get(token_env):
    sys.exit(f"{token_env} is not set. Attach your source API token as a capsule secret with that env var name.")
os.environ["SRC_CO_TOKEN"] = os.environ[token_env]            # the git credential helper reads SRC_CO_TOKEN
if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", bundle) or bundle in RESERVED_BUNDLES:
    sys.exit(f"bundle must be lowercase letters, digits and hyphens (e.g. team-dev-skills), and not "
             f"{', '.join(sorted(RESERVED_BUNDLES))}; got {bundle!r}")

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib would resend the Authorization header to a redirect target, even on another host.
    def redirect_request(self, *a, **kw):
        return None

_ctx = ssl.create_default_context(cafile=SYSTEM_CA) if os.path.isfile(SYSTEM_CA) else ssl.create_default_context()
_opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=_ctx))

def token_owner_email():
    # The API has no "whoami". Everything a search for "ownership: private" returns belongs to the
    # token's owner, so the first owned capsule or data asset gives us the owner's email.
    auth = "Basic " + base64.b64encode((os.environ["SRC_CO_TOKEN"] + ":").encode()).decode()
    errors = []
    for kind in ("capsules", "data_assets"):
        req = urllib.request.Request(f"{SRC}/api/v1/{kind}/search", method="POST",
                                     data=json.dumps({"ownership": "private", "limit": 1}).encode(),
                                     headers={"Authorization": auth, "Content-Type": "application/json"})
        try:
            with _opener.open(req, timeout=60) as r:
                found = (json.load(r).get("results") or [{}])[0].get("owner_email")
        except Exception as e:
            errors.append(f"{kind} search: {e}")
            continue
        if found:
            return found, errors
    return "", errors

if not git_user:
    git_user, errors = token_owner_email()
    if not git_user:
        why = ("the source rejected the token (" + "; ".join(errors) + "). Check the secret"
               if any("401" in e or "403" in e for e in errors)
               else "; ".join(errors) if errors else "you own no capsules or data assets on the source")
        sys.exit(f"Couldn't work out your email from the token: {why}. "
                 "Or set git_user to the email of the account that owns the token.")
    print(f"Using git_user {git_user}, the owner of the source token.")
os.environ.update(SRC_CO_USER=git_user, SRC_CO_HOSTPORT=urlsplit(SRC).netloc.lower(), GIT_TERMINAL_PROMPT="0")
RESULTS = pathlib.Path(os.environ.get("RESULTS_DIR", "/results"))       # override for local tests
out, work = RESULTS / bundle, pathlib.Path(tempfile.mkdtemp())
atexit.register(shutil.rmtree, work, True)
if out.exists():
    print(f"Overwriting existing {out}")
    shutil.rmtree(out)

# The token reaches git from the environment through this helper, never argv. The helper answers only for the
# source host, and git doesn't follow redirects, so the token can't be sent anywhere else.
_HELPER = ('!f() { test "$1" = get || exit 0; h=; '
           'while IFS= read -r l && [ -n "$l" ]; do case "$l" in host=*) h=${l#host=};; esac; done; '
           '[ "$h" = "$SRC_CO_HOSTPORT" ] || exit 0; '
           'printf "username=%s\\npassword=%s\\n" "$SRC_CO_USER" "$SRC_CO_TOKEN"; }; f')

def git(*args, cwd=None):
    return subprocess.run(["git", "-c", "credential.helper=", "-c", "credential.helper=" + _HELPER,
                           "-c", "http.followRedirects=false", *args],
                          cwd=cwd, check=True, capture_output=True, text=True, timeout=600).stdout

def hidden(path, root):
    return any(part.startswith(".") for part in path.relative_to(root).parts)

def skill_dirs(repo):
    if (repo / "SKILL.md").is_file() and not (repo / "SKILL.md").is_symlink():
        return [repo]                                                    # single skill
    # Bundle: each folder with a SKILL.md is a skill. Hidden folders (.git, .archive, ...) are never skills.
    found = sorted(p.parent for p in repo.rglob("SKILL.md")
                   if p.is_file() and not p.is_symlink() and not hidden(p, repo))
    return [d for d in found if not any(o in d.parents for o in found)]  # bundle children

def frontmatter(skill_dir):
    # Returns (fields, None) or (None, reason) so one bad SKILL.md never stops the run.
    try:
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8-sig")
        m = re.match(r"---[ \t]*\r?\n(.*?)^---[ \t]*\r?$", text, re.S | re.M)
        fm = yaml.safe_load(m.group(1)) if m else None
    except (OSError, ValueError, RecursionError, yaml.YAMLError) as e:
        return None, f"unreadable SKILL.md frontmatter: {(str(e).splitlines() or [type(e).__name__])[0]}"
    if not isinstance(fm, dict):
        return None, "SKILL.md has no YAML frontmatter between --- lines"
    if not isinstance(fm.get("name"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", fm["name"]):
        return None, f"SKILL.md name is missing or not a valid folder name: {fm.get('name')!r}"
    return fm, None

def tags_of(fm):
    meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
    tags = meta.get("tags") if meta.get("tags") is not None else fm.get("tags")  # metadata.tags, else top-level
    if isinstance(tags, str):
        tags = re.split(r"[,\s]+", tags)
    elif not isinstance(tags, (list, tuple, set)):
        tags = [tags] if tags not in (None, False, "") else []
    return {str(t).strip().casefold() for t in tags if not isinstance(t, (dict, list))}

def slug_of(item):
    # A URL must point at /capsule/NNNNNNN; anything else must be exactly 7 digits.
    m = re.search(r"/capsule/(\d{7})(?!\d)", item) if ("://" in item or "/capsule/" in item) else \
        re.fullmatch(r"(\d{7})", item)
    return m.group(1) if m else None

def copy_skill(src, dest):
    # Copy regular files and folders only. Symbolic links are dropped, never followed: a link to
    # /proc/self/environ or a mounted secret would otherwise land in the results. So are file names that
    # can't be written as one SHA256SUMS line.
    dropped = []
    def ignore(folder, names):
        bad = {n for n in names if n == ".git" or os.path.islink(os.path.join(folder, n))
               or "\n" in n or "\r" in n}
        dropped.extend(os.path.relpath(os.path.join(folder, n), src) for n in sorted(bad) if n != ".git")
        return bad
    shutil.copytree(src, dest, symlinks=False, ignore=ignore)
    return dropped

packed, skipped, slugs = [], [], {}
for item in filter(None, re.split(r"[\s,;]+", skills_arg)):  # slugs or pasted URLs
    slug, host = slug_of(item), (urlsplit(item).hostname if "://" in item else None)
    if not slug:
        skipped.append({"input": item, "reason": "not a 7-digit slug or /capsule/NNNNNNN URL"})
    elif host and host != urlsplit(SRC).hostname:          # slugs differ between deployments
        skipped.append({"slug": slug, "reason": f"URL is on {host}, not the source {SRC}"})
    else:
        slugs.setdefault(slug)

for slug in slugs:
    repo = work / slug
    try:
        git("clone", "-q", f"{SRC}/capsule-{slug}.git", str(repo))
    except subprocess.TimeoutExpired:
        skipped.append({"slug": slug, "reason": "clone failed: timed out after 600 s"})
        continue
    except subprocess.CalledProcessError as e:
        msg = " / ".join(l for l in (e.stderr or "").splitlines() if l.strip())
        skipped.append({"slug": slug, "reason": f"clone failed: {msg[:300] or e}"})
        continue
    try:
        sha = git("rev-parse", "--short", "HEAD", cwd=repo).strip()
    except subprocess.SubprocessError:
        skipped.append({"slug": slug, "reason": "no commits; commit the skill on the source"})
        continue
    if (repo / ".codeocean").is_dir():                        # skill repos never have .codeocean/
        skipped.append({"slug": slug, "reason": "regular capsule, not a skill (it has a .codeocean folder)"})
        continue
    dirs = skill_dirs(repo)
    if not dirs:
        skipped.append({"slug": slug, "reason": "no SKILL.md in the repo; not a skill"})
    for d in dirs:
        where = "/" + "/".join(d.relative_to(repo).parts)
        fm, why = frontmatter(d)
        name = fm and fm["name"]
        if why:
            skipped.append({"slug": slug, "path": where, "reason": why})
        elif keyword and keyword.casefold() not in tags_of(fm):
            skipped.append({"slug": slug, "name": name, "reason": f"not tagged {keyword}"})
        elif any(p["name"].casefold() == name.casefold() for p in packed):
            first = next(p["slug"] for p in packed if p["name"].casefold() == name.casefold())
            skipped.append({"slug": slug, "name": name, "reason": f"duplicate name; already packed from {first}"})
        else:
            dest = out / "skills" / name
            dropped = copy_skill(d, dest)
            files = sorted(p for p in dest.rglob("*") if p.is_file())
            entry = {"name": name, "slug": slug, "path": where, "commit": sha, "files": len(files),
                     "bytes": sum(p.stat().st_size for p in files),
                     # Aqua can't list result subfolders, so give it every file path to read.
                     "paths": [f"{bundle}/skills/{name}/{p.relative_to(dest).as_posix()}" for p in files],
                     "executables": [p.relative_to(dest).as_posix() for p in files
                                     if p.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)]}
            if dropped:
                entry["not_copied"] = dropped
                print(f"{name}: not copied (symbolic links or unusable file names): {', '.join(dropped)}")
            packed.append(entry)

RESULTS.mkdir(parents=True, exist_ok=True)
(RESULTS / "report.json").write_text(json.dumps(
    {"packer_version": VERSION, "source": SRC, "bundle": bundle, "keyword": keyword,
     "packed": packed, "skipped": skipped}, indent=2))
print(json.dumps({"packed": [p["name"] for p in packed], "skipped": skipped}, indent=2))
if not packed:
    sys.exit("Nothing packed. " + ("See the reasons above (also in report.json)." if skipped
                                   else "No 7-digit capsule slugs found in skills."))

today = datetime.datetime.now(datetime.timezone.utc).date().isoformat() + " (UTC)"
(out / ".claude-plugin").mkdir(parents=True)
(out / ".claude-plugin" / "plugin.json").write_text(json.dumps(
    {"name": bundle, "version": "1.0.0", "description": f"Skills packed from {SRC} on {today}"}, indent=2) + "\n")
(out / "README.md").write_text(f"# {bundle}\n\nPacked from {SRC} on {today} by Skill Packer {VERSION}.\n\n"
    "| Skill | Source slug | Commit | Files | Bytes |\n|---|---|---|---|---|\n" + "".join(
    f"| {p['name']} | {p['slug']} | {p['commit']} | {p['files']} | {p['bytes']} |\n" for p in packed))
(out / "CHANGELOG.md").write_text(f"## {today}\n\n- Packed {len(packed)} skill(s) from {SRC}, skipped {len(skipped)}"
    + (f" (filter: tag {keyword})" if keyword else "") + ".\n"
    + "".join(f"- {p['name']} from {p['slug']} @ {p['commit']}\n" for p in packed))

# Checksums let a verify step catch any file that changes on its way into the new skills. A backslash in a
# name is escaped the way sha256sum -c expects (the line then starts with a backslash).
def sum_line(f):
    rel = f.relative_to(out).as_posix()
    esc = rel.replace("\\", "\\\\")
    return ("\\" if esc != rel else "") + f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {esc}\n"
(out / "SHA256SUMS").write_text("".join(sum_line(f) for f in sorted((out / "skills").rglob("*")) if f.is_file()))
