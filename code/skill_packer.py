#!/usr/bin/env python3
# Skill Packer: clone skills from a source Code Ocean deployment and write them
# to /results in Claude plugin layout, ready for Aqua to unpack into skills.
import argparse, atexit, base64, datetime, hashlib, json, os, pathlib, re, shutil, subprocess, sys, tempfile
import urllib.request
from urllib.parse import urlsplit
import yaml

# App Panel named parameters arrive as --name value, so an empty optional field can't shift the rest.
ap = argparse.ArgumentParser(description="Pack Code Ocean skills into a Claude plugin layout.")
ap.add_argument("--skills", required=True, help="slugs or /capsule/NNNNNNN URLs")
ap.add_argument("--git_user", default="", help="the token owner's email; looked up from the token when empty")
ap.add_argument("--keyword", default="", help="optional tag filter")
ap.add_argument("--bundle", default="migrated-skills", help="output folder and plugin name")
ap.add_argument("--source_host", default="", help="source deployment URL; defaults to the SRC_HOST env var")
ap.add_argument("--token_env", default="", help="env var holding the source API token; defaults to SRC_CO_TOKEN")
args = ap.parse_args()
skills_arg, git_user, keyword, bundle = (args.skills.strip(), args.git_user.strip(),
                                         args.keyword.strip(), args.bundle.strip() or "migrated-skills")
SRC = (args.source_host.strip() or os.environ.get("SRC_HOST", "")).rstrip("/")
if not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d+)?", SRC):
    sys.exit("Set the source deployment as https://<host>, in the source_host parameter or the SRC_HOST env var.")
token_env = args.token_env.strip() or "SRC_CO_TOKEN"
if not os.environ.get(token_env):
    sys.exit(f"{token_env} is not set. Attach your source API token as a capsule secret with that env var name.")
os.environ["SRC_CO_TOKEN"] = os.environ[token_env]            # the git credential helper reads SRC_CO_TOKEN
if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", bundle):
    sys.exit(f"bundle must be lowercase letters, digits and hyphens (e.g. team-dev-skills), got {bundle!r}")

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
            with urllib.request.urlopen(req, timeout=60) as r:
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
os.environ.update(SRC_CO_USER=git_user, GIT_TERMINAL_PROMPT="0")  # git wants the token owner's email
RESULTS = pathlib.Path(os.environ.get("RESULTS_DIR", "/results"))       # override for local tests
out, work = RESULTS / bundle, pathlib.Path(tempfile.mkdtemp())
atexit.register(shutil.rmtree, work, True)
if out.exists():
    print(f"Overwriting existing {out}")
    shutil.rmtree(out)

def git(*args, cwd=None):
    # The token comes from a capsule secret through the environment, never argv.
    helper = '!f() { echo "username=$SRC_CO_USER"; echo "password=$SRC_CO_TOKEN"; }; f'
    return subprocess.run(["git", "-c", "credential.helper=", "-c", "credential.helper=" + helper,
                           *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=600).stdout

def skill_dirs(repo):
    if (repo / "SKILL.md").is_file():
        return [repo]                                                    # single skill
    found = sorted(p.parent for p in repo.rglob("SKILL.md")
                   if p.is_file() and ".git" not in p.relative_to(repo).parts)
    return [d for d in found if not any(o in d.parents for o in found)]  # bundle children

def frontmatter(skill_dir):
    # Returns (fields, None) or (None, reason) so one bad SKILL.md never stops the run.
    try:
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8-sig")
        m = re.match(r"---[ \t]*\r?\n(.*?)^---[ \t]*\r?$", text, re.S | re.M)
        fm = yaml.safe_load(m.group(1)) if m else None
    except (OSError, ValueError, yaml.YAMLError) as e:
        return None, f"unreadable SKILL.md frontmatter: {str(e).splitlines()[0]}"
    if not isinstance(fm, dict):
        return None, "SKILL.md has no YAML frontmatter between --- lines"
    if not isinstance(fm.get("name"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", fm["name"]):
        return None, f"SKILL.md name is missing or not a valid folder name: {fm.get('name')!r}"
    return fm, None

def tags_of(fm):
    tags = (fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}).get("tags") or []
    tags = re.split(r"[,\s]+", tags) if isinstance(tags, str) else tags
    return {str(t).strip().casefold() for t in tags}

packed, skipped, slugs = [], [], {}
for item in filter(None, re.split(r"[\s,;]+", skills_arg)):  # slugs or pasted URLs
    m, host = re.search(r"\b\d{7}\b", item), urlsplit(item).hostname if "://" in item else None
    if not m:
        skipped.append({"input": item, "reason": "not a 7-digit slug or /capsule/NNNNNNN URL"})
    elif host and host != urlsplit(SRC).hostname:          # slugs differ between deployments
        skipped.append({"slug": m.group(), "reason": f"URL is on {host}, not the source {SRC}"})
    else:
        slugs.setdefault(m.group())

for slug in slugs:
    repo = work / slug
    try:
        git("clone", "-q", f"{SRC}/capsule-{slug}.git", str(repo))
    except subprocess.SubprocessError as e:
        msg = " / ".join(l for l in (getattr(e, "stderr", "") or "").splitlines() if l.strip())
        skipped.append({"slug": slug, "reason": f"clone failed: {msg[:300] or e}"})
        continue
    try:
        sha = git("rev-parse", "--short", "HEAD", cwd=repo).strip()
    except subprocess.CalledProcessError:
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
        elif any(p["name"] == name for p in packed):
            first = next(p["slug"] for p in packed if p["name"] == name)
            skipped.append({"slug": slug, "name": name, "reason": f"duplicate name; already packed from {first}"})
        else:
            dest = out / "skills" / name
            shutil.copytree(d, dest, symlinks=True, ignore=shutil.ignore_patterns(".git"))
            files = [p for p in dest.rglob("*") if p.is_file() or p.is_symlink()]
            # Aqua can't list result subfolders, so give it every file path to read.
            packed.append({"name": name, "slug": slug, "path": where, "commit": sha, "files": len(files),
                           "bytes": sum(p.lstat().st_size for p in files),
                           "paths": sorted(f"{bundle}/skills/{name}/{p.relative_to(dest).as_posix()}" for p in files)})

RESULTS.mkdir(parents=True, exist_ok=True)
(RESULTS / "report.json").write_text(json.dumps(
    {"source": SRC, "bundle": bundle, "keyword": keyword, "packed": packed, "skipped": skipped}, indent=2))
print(json.dumps({"packed": [p["name"] for p in packed], "skipped": skipped}, indent=2))
if not packed:
    sys.exit("Nothing packed. " + ("See the reasons above (also in report.json)." if skipped
                                   else "No 7-digit capsule slugs found in skills."))

today = datetime.date.today().isoformat()
(out / ".claude-plugin").mkdir(parents=True)
(out / ".claude-plugin" / "plugin.json").write_text(json.dumps(
    {"name": bundle, "version": "1.0.0", "description": f"Skills packed from {SRC} on {today}"}, indent=2) + "\n")
(out / "README.md").write_text(f"# {bundle}\n\nPacked from {SRC} on {today}.\n\n"
    "| Skill | Source slug | Commit | Files | Bytes |\n|---|---|---|---|---|\n" + "".join(
    f"| {p['name']} | {p['slug']} | {p['commit']} | {p['files']} | {p['bytes']} |\n" for p in packed))
(out / "CHANGELOG.md").write_text(f"## {today}\n\n- Packed {len(packed)} skill(s) from {SRC}, skipped {len(skipped)}"
    + (f" (filter: tag {keyword})" if keyword else "") + ".\n"
    + "".join(f"- {p['name']} from {p['slug']} @ {p['commit']}\n" for p in packed))
# Checksums let a verify step catch any file that changes on its way into the new skills.
sums = [f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {f.relative_to(out).as_posix()}\n"
        for f in sorted((out / "skills").rglob("*")) if f.is_file() and not f.is_symlink()]
(out / "SHA256SUMS").write_text("".join(sums))
