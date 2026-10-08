"""Offline tests for skill_bridge (no network). Run: python -m pytest tests"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import skill_bridge as sb  # noqa: E402

U1, U2, U3 = ("0a1b2c3d-0000-4000-8000-000000000001", "0a1b2c3d-0000-4000-8000-000000000002",
              "0a1b2c3d-0000-4000-8000-000000000003")


# -- prompts -------------------------------------------------------------------------------------------------------

def test_pack_prompt_exact_wording():
    got = sb.build_pack_prompt(["2000001", "0100003"], "c0ffee00-0000-4000-8000-000000000000")
    assert got == (
        "Run the Skill Packer capsule c0ffee00-0000-4000-8000-000000000000 with skills = 2000001, 0100003. When it "
        "finishes, read report.json from the run's results. Before creating anything, compare each name in its "
        "\"packed\" list with the custom skills I already have, and skip any packed skill whose name matches one of "
        "mine. For each remaining skill, create a separate stand-alone skill whose files are exact byte-for-byte "
        "copies of the files listed in that skill's \"paths\", keeping each file's path relative to its skill "
        "folder. Do not edit, reformat or improve anything. Commit each skill. Then list each new skill's name and "
        "slug, the skills you skipped because I already have them, the computation ID, and anything the packer "
        "skipped with its reason.")


def test_pack_prompt_placeholder_without_packer_id():
    assert "Skill Packer capsule <PACKER_CAPSULE_ID> with skills = 1234567." in sb.build_pack_prompt(["1234567"])


def test_unpack_prompt_exact_wording():
    assert sb.build_unpack_prompt("abc-123") == (
        "Computation abc-123 (a run of the Skill Packer capsule) has results under migrated-skills/ in Claude plugin "
        "layout. Read report.json from the results. For each skill in its \"packed\" list, create a separate "
        "stand-alone skill whose files are exact byte-for-byte copies of the files listed in that skill's \"paths\", "
        "keeping each file's path relative to its skill folder. Do not edit, reformat or improve anything. Commit "
        "each skill. Report each new skill's name and slug.")


def test_list_prompt_is_the_tested_wording():
    assert sb.LIST_PROMPT.startswith("Activate every skill in your available-skills list with your skills tool")
    assert sb.LIST_PROMPT.endswith("Do not create, change or delete anything.")


# -- small helpers -------------------------------------------------------------------------------------------------

def test_parse_slugs_accepts_urls_and_separators():
    text = "2000001, https://x.example.com/capsule/0100003/tree\n5000002;2000001 nope 12345678"
    assert sb.parse_slugs(text) == ["2000001", "0100003", "5000002"]


@pytest.mark.parametrize("raw,want", [("example.com", "https://example.com"), ("https://example.com/", "https://example.com"),
                                      ("", "")])
def test_normalize_host(raw, want):
    assert sb.normalize_host(raw) == want


def test_normalize_host_rejects_paths():
    with pytest.raises(sb.BridgeError):
        sb.normalize_host("https://example.com/some/path")


# -- Aqua stream and listing ---------------------------------------------------------------------------------------

def sse(*events):
    out = []
    for kind, data in events:
        out += [f"event: {kind}", "data: " + json.dumps(data), ""]
    return out


def test_parse_sse_reply_tools_and_reasoning():
    reply, events = sb.parse_sse(sse(("reasoning", {"data": "Think"}), ("reasoning", {"data": "ing"}),
                                     ("tool_use", {"name": "skills", "explanation": "x", "input": {"skill_name": "a"}}),
                                     ("data", {"data": "Hel"}), ("data", {"data": "lo"})))
    assert reply == "Hello"
    assert events[0] == {"type": "thinking", "text": "Thinking"}
    assert events[1] == {"type": "tool_call", "name": "skills", "input": {"skill_name": "a"}}


@pytest.mark.parametrize("kind", ["error", "questions"])
def test_parse_sse_error_and_question_events_raise(kind):
    with pytest.raises(sb.BridgeError):
        sb.parse_sse(sse((kind, {"data": "boom"})))


REPLY_B = """All eleven skills activated.

| Skill | Loaded from capsule |
|---|---|
| science | none |

```json
[
  {"skill": "report-builder", "capsule_id": "%s", "name": "report-builder", "slug": "2000001"},
  {"skill": "smoke-test", "capsule_id": "%s", "name": "smoke-test", "slug": "5000002"},
  {"skill": "qc-filter", "capsule_id": "%s", "name": "qc-filter", "slug": "0100003"}
]
```

Links: report-builder [2000001](/capsule/2000001)""" % (U1, U2, U3)


def test_parse_listing_skill_and_capsule_id_keys():
    events = [{"type": "tool_call", "name": "skills", "input": {"skill_name": n}}
              for n in ("science", "report-builder", "smoke-test", "qc-filter")]
    entries, available = sb.parse_listing(REPLY_B, events)
    assert [(e["name"], e["slug"], e["uuid"]) for e in entries] == [
        ("report-builder", "2000001", U1), ("smoke-test", "5000002", U2),
        ("qc-filter", "0100003", U3)]
    assert available == ["science", "report-builder", "smoke-test", "qc-filter"]


def test_parse_listing_uuid_name_slug_keys_and_numeric_slug():
    reply = 'x [{"uuid": "%s", "name": "qc-filter", "slug": 100003}] y' % U3
    entries, _ = sb.parse_listing(reply, [])
    assert entries == [{"name": "qc-filter", "capsule_name": "qc-filter", "uuid": U3,
                        "slug": "0100003"}]


def test_parse_listing_falls_back_to_get_capsule_calls():
    events = [{"type": "tool_call", "name": "get_capsule", "input": {"capsule_id": U1}},
              {"type": "tool_call", "name": "get_capsule", "input": {"capsule_id": U1}}]
    entries, _ = sb.parse_listing("No JSON here, sorry.", events)
    assert entries == [{"name": "", "capsule_name": "", "uuid": U1, "slug": None}]


def test_parse_listing_ignores_none_rows():
    reply = '[{"skill": "science", "capsule_id": "none"}, {"skill": "a", "uuid": "%s", "slug": "2000001"}]' % U1
    entries, _ = sb.parse_listing(reply, [])
    assert [e["slug"] for e in entries] == ["2000001"]


class FakeClient:
    def __init__(self, caps):
        self.caps = caps

    def get_capsule(self, uuid):
        if uuid not in self.caps:
            raise sb.BridgeError("Not found (HTTP 404).")
        return self.caps[uuid]


def test_verify_entries_prefers_api_slug_and_fills_names():
    entries = [{"name": "", "capsule_name": "", "uuid": U1, "slug": None},
               {"name": "x", "capsule_name": "x", "uuid": U2, "slug": "1111111"},
               {"name": "y", "capsule_name": "y", "uuid": U3, "slug": "0100003"}]
    notes = sb.verify_entries(FakeClient({U1: {"slug": "2000001", "name": "report-builder"},
                                          U2: {"slug": "5000002", "name": "smoke-test"}}), entries)
    assert entries[0]["slug"] == "2000001" and entries[0]["name"] == "report-builder"
    assert entries[1]["slug"] == "5000002"
    assert entries[2]["slug"] == "0100003"                       # API miss keeps Aqua's value
    assert len(notes) == 2


def test_listing_names_include_capsule_and_available_names():
    lst = sb.Listing(skills=[{"name": "Alpha", "capsule_name": "alpha-capsule", "uuid": U1, "slug": "1"}],
                     available=["science"])
    assert lst.names() == {"alpha", "alpha-capsule", "science"}


# -- reading skill repos -------------------------------------------------------------------------------------------

def write_skill(d: Path, name: str, desc: str = "Does things.", tags="[a, b]", extra: dict | None = None):
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\nmetadata:\n  tags: {tags}\n---\n# {name}\n")
    for rel, text in (extra or {}).items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text)


def test_read_single_skill(tmp_path):
    write_skill(tmp_path, "alpha", "x" * 300, extra={"references/notes.md": "hello"})
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("ignored")
    row = sb.read_skill_repo(tmp_path, sb.new_row("1234567"))
    assert row["names"] == ["alpha"] and row["tags"] == ["a", "b"] and row["files"] == 2
    assert len(row["description"]) == sb.DESC_MAX and row["description"].endswith("…")
    assert not row["problems"] and sb.default_selected(row)


def test_read_bundle(tmp_path):
    write_skill(tmp_path / "skills" / "one", "one", tags="[x]")
    write_skill(tmp_path / "skills" / "two", "two", tags="y, X")
    row = sb.read_skill_repo(tmp_path, sb.new_row("1234567"))
    assert row["names"] == ["one", "two"] and row["tags"] == ["x", "y"]
    assert row["notes"] == ["bundle of 2 skills"] and sb.default_selected(row)


def test_regular_capsule_is_not_a_skill(tmp_path):
    write_skill(tmp_path, "looks-like-a-skill")
    (tmp_path / ".codeocean").mkdir()
    row = sb.read_skill_repo(tmp_path, sb.new_row("1234567"))
    assert row["problems"][0].startswith("not a skill") and sb.is_blocked(row) and not sb.default_selected(row)


def test_no_skill_md_and_bad_frontmatter(tmp_path):
    row = sb.read_skill_repo(tmp_path, sb.new_row("1234567"))
    assert row["problems"] == ["no SKILL.md in the repo"] and sb.is_blocked(row)
    (tmp_path / "SKILL.md").write_text("no frontmatter here")
    row = sb.read_skill_repo(tmp_path, sb.new_row("1234567"))
    assert "frontmatter" in row["problems"][0] and sb.is_blocked(row)


def test_enrich_empty_repo_is_not_committed(tmp_path, monkeypatch):
    bare = tmp_path / "capsule-7654321.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    monkeypatch.setattr(sb, "normalize_host", lambda h: h)        # let the test clone from a file:// URL
    row = sb.enrich(f"file://{tmp_path}", "7654321", "user@example.com", "not-a-real-token")
    assert row["problems"] == ["not committed: commit the skill on the source"]


def test_enrich_committed_repo(tmp_path, monkeypatch):
    work = tmp_path / "work"
    write_skill(work, "gamma")
    run = lambda *a: subprocess.run(["git", *a], cwd=work, check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    run("add", ".")
    run("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "x")
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(tmp_path / "capsule-7654321.git")], check=True)
    monkeypatch.setattr(sb, "normalize_host", lambda h: h)
    row = sb.enrich(f"file://{tmp_path}", "7654321", "user@example.com", "not-a-real-token")
    assert row["names"] == ["gamma"] and len(row["commit"]) >= 7 and not row["problems"]


def test_mark_on_target_and_status():
    row = sb.new_row("1234567")
    row["names"] = ["Alpha"]
    sb.mark_on_target([row], {"alpha"})
    assert row["on_target"] == ["Alpha"] and not sb.default_selected(row)
    assert sb.row_status(row) == "already on target"


# -- packer --------------------------------------------------------------------------------------------------------

PANEL_OLD = [{"param_name": "skills", "required": True}, {"param_name": "git_user", "required": True},
             {"param_name": "source_host"}, {"param_name": "token_env", "default_value": "MY_KEY"}]
PANEL_NEW = [{"param_name": "skills", "required": True}, {"param_name": "git_user"}]


def test_packer_parameters_follow_the_app_panel():
    assert sb.packer_parameters(PANEL_OLD, ["1", "2"], "https://src.example.com", "me@example.com") == [
        {"param_name": "skills", "value": "1, 2"}, {"param_name": "source_host", "value": "https://src.example.com"},
        {"param_name": "git_user", "value": "me@example.com"}]
    assert sb.packer_parameters(PANEL_NEW, ["1"], "https://src.example.com", "me@example.com", "KEY") == [
        {"param_name": "skills", "value": "1"}]
    assert sb.packer_parameters(PANEL_OLD, ["1"], token_env="KEY")[-1] == {"param_name": "token_env", "value": "KEY"}


def test_parse_packer_output():
    out = 'Using git_user me@example.com, the owner of the source token.\n{\n  "packed": [\n    "alpha"\n  ],\n' \
          '  "skipped": [{"slug": "1234567", "reason": "no commits"}]\n}\n'
    got = sb.parse_packer_output(out)
    assert got["packed"] == ["alpha"] and got["skipped"][0]["slug"] == "1234567"
    assert got["messages"].startswith("Using git_user")
    assert sb.parse_packer_output("SRC_CO_TOKEN is not set.")["messages"] == "SRC_CO_TOKEN is not set."


class FakeDst:
    def __init__(self, comp, files):
        self.comp, self.files, self.started = comp, files, None

    def start_computation(self, capsule_id, params):
        self.started = (capsule_id, params)
        return "comp-1"

    def get_computation(self, cid):
        return self.comp

    def result_text(self, cid, path):
        return self.files.get(path)


def test_run_packer_uses_exit_code_not_end_status():
    dst = FakeDst({"state": "completed", "end_status": "succeeded", "exit_code": 1, "has_results": False},
                  {"output": "NOPE is not set."})
    res = sb.run_packer(dst, "cap", [{"param_name": "skills", "value": "1"}], poll=0)
    assert res["ok"] is False and res["exit_code"] == 1 and res["output"] == "NOPE is not set."


def test_run_packer_reads_report():
    report = {"bundle": "migrated-skills", "packed": [{"name": "alpha", "slug": "1234567", "commit": "abc1234",
                                                       "files": 1, "bytes": 10, "paths": ["x"]}], "skipped": []}
    dst = FakeDst({"state": "completed", "end_status": "succeeded", "exit_code": 0, "has_results": True},
                  {"output": '{\n"packed": ["alpha"], "skipped": []}', "report.json": json.dumps(report)})
    res = sb.run_packer(dst, "cap", [], poll=0)
    assert res["ok"] and res["packed"][0]["commit"] == "abc1234" and res["bundle"] == "migrated-skills"


def test_check_without_config_fails_cleanly(monkeypatch):
    for k in ("SRC_HOST", "DST_HOST", "SRC_CO_TOKEN", "DST_CO_TOKEN", "PACKER_CAPSULE_ID"):
        monkeypatch.delenv(k, raising=False)
    ok, lines = sb.check(sb.Config.from_env())
    assert not ok
    assert any(l.startswith("FAIL source: URL and token (SRC_CO_TOKEN) not set") for l in lines)
    assert any(l.startswith("-    target:") and "(optional)" in l for l in lines)


# -- environment and Git safety ------------------------------------------------------------------------------------

def test_use_system_ca(tmp_path, monkeypatch):
    bundle = tmp_path / "ca-certificates.crt"
    env = {}
    sb.use_system_ca(env, str(bundle))                                # no system bundle: nothing changes
    assert env == {}
    bundle.write_text("-----BEGIN CERTIFICATE-----\n")
    sb.use_system_ca(env, str(bundle))
    assert env == {"REQUESTS_CA_BUNDLE": str(bundle)}
    for key in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "CURL_CA_BUNDLE"):   # a bundle the user set wins
        env = {key: "/custom/ca.pem"}
        sb.use_system_ca(env, str(bundle))
        assert env == {key: "/custom/ca.pem"}
    # requests reads the variable on every request, the plain GET of a pre-signed URL included.
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle))
    got = requests.Session().merge_environment_settings("https://bucket.example.com/f?sig=1", {}, None, None, None)
    assert got["verify"] == str(bundle)


def test_git_credentials_only_go_to_the_clone_host():
    env = sb._git_env("me@example.com", "not-a-real-token", "https://git.example.com:8443")

    def ask(host, action="fill"):
        return subprocess.run(["git", "-c", "credential.helper=", "-c", "credential.helper=" + sb._HELPER,
                               "credential", action], input=f"protocol=https\nhost={host}\n\n", env=env,
                              capture_output=True, text=True)
    good = ask("git.example.com:8443")
    assert "username=me@example.com" in good.stdout and "password=not-a-real-token" in good.stdout
    for host in ("evil.example.com", "git.example.com", "git.example.com:8443.evil.example.com"):
        bad = ask(host)
        assert bad.returncode != 0 and "not-a-real-token" not in bad.stdout + bad.stderr
    assert ask("git.example.com:8443", "approve").stdout == ""      # only "get" is answered


def test_git_never_follows_redirects_and_keeps_the_token_out_of_argv(monkeypatch):
    seen = []

    def run(cmd, **kw):
        seen.append((cmd, kw["env"]))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(sb.subprocess, "run", run)
    sb._git(["ls-remote", "https://git.example.com/capsule-1234567.git"],
            sb._git_env("me@example.com", "not-a-real-token", "https://git.example.com"))
    cmd, env = seen[0]
    assert cmd[cmd.index("http.followRedirects=false") - 1] == "-c"
    assert not any("not-a-real-token" in part for part in cmd) and env["SB_GIT_HOST"] == "git.example.com"


def test_api_calls_do_not_follow_redirects(monkeypatch):
    def respond(self, method, url, **kw):
        assert kw["allow_redirects"] is False
        r = requests.Response()
        r.status_code, r.url, r._content, r._content_consumed = 302, url, b"", True
        r.headers["Location"] = "https://login.example.net/sso"
        return r
    monkeypatch.setattr(requests.Session, "request", respond)
    client = sb.CoClient("https://target.example.com", "not-a-real-token", "target")
    with pytest.raises(sb.BridgeError, match="redirected GET /api/v1/computations/c1 to login.example.net"):
        client.get_computation("c1")


def test_slug_parsing_is_strict():
    text = ("https://x.example.com/capsule/1234567/tree?ref=7654321 abc1234567def 12345678 id=2345678 (3456789), "
            "`4567890`. [5678901](/capsule/5678901) https://x.example.com/capsule-6789012.git "
            "0a1b2c3d-0000-4000-8000-000000000001 https://x.example.com/data/7777777")
    assert sb.parse_slugs(text) == ["1234567", "3456789", "4567890", "5678901", "6789012"]


def test_slugs_on_host_leaves_out_other_deployments():
    slugs, other = sb.slugs_on_host("1111111, https://t.example.com/capsule/2222222/tree "
                                    "[3333333](https://s.example.com/capsule/3333333)", "https://t.example.com")
    assert slugs == ["1111111", "2222222"] and other == ["[3333333](https://s.example.com/capsule/3333333)"]


def test_linked_skill_md_is_never_read(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("---\nname: leaked\ndescription: TOP-SECRET-VALUE\n---\n")
    single = tmp_path / "single"
    single.mkdir()
    (single / "SKILL.md").symlink_to(secret)
    row = sb.read_skill_repo(single, sb.new_row("1234567"))
    assert row["problems"] == ["not a skill (SKILL.md is a link)"] and sb.is_blocked(row) and not row["names"]
    bundle = tmp_path / "bundle"                                      # a linked child; links aren't counted
    write_skill(bundle / "skills" / "good", "good")
    (bundle / "skills" / "bad").mkdir()
    (bundle / "skills" / "bad" / "SKILL.md").symlink_to(secret)
    (bundle / "skills" / "good" / "env").symlink_to("/proc/self/environ")
    row = sb.read_skill_repo(bundle, sb.new_row("1234567"))
    assert row["names"] == ["good"] and row["files"] == 1 and sb.is_blocked(row)
    assert row["problems"] == ["/skills/bad: not a skill (SKILL.md is a link)"]
    assert "TOP-SECRET-VALUE" not in json.dumps(row)


def test_hidden_folders_never_hold_the_skill(tmp_path):
    write_skill(tmp_path / ".archive" / "beta", "beta", "Old archived beta")
    write_skill(tmp_path / "skills" / "beta", "beta", "Current beta")
    assert sb.skill_dirs(tmp_path) == [tmp_path / "skills" / "beta"]
    row = sb.read_skill_repo(tmp_path, sb.new_row("1234567"))
    assert row["names"] == ["beta"] and row["description"] == "Current beta" and not row["notes"]


@pytest.mark.parametrize("tags,want", [(5, ["5"]), (True, []), ({"a": 1}, []), (None, []), ("a, b", ["a", "b"]),
                                       ([1, "x", True, {"k": 1}, None, "X"], ["1", "x"])])
def test_tags_of_tolerates_odd_values(tags, want):
    assert sb.tags_of({"metadata": {"tags": tags}}) == want


def test_deep_yaml_is_a_frontmatter_problem_not_a_crash():
    fm, why = sb.parse_frontmatter("---\nname: x\ndeep: " + "[" * 3000 + "]" * 3000 + "\n---\n")
    assert fm is None and why.startswith("unreadable SKILL.md frontmatter")


# -- checking the copies -------------------------------------------------------------------------------------------

CID = "c0de0000-0000-4000-8000-0000000000aa"
TEXT_MD = ("---\nname: text-skill\ndescription: Replies with a marker.\n---\n# Text skill\n"
           "Quote “exactly” — then stop. Café.\n").encode()
MULTI = {"SKILL.md": b"---\nname: multi-skill\ndescription: Uses its files.\n---\nRead references/guide.md.\n",
         "references/guide.md": "# Guide\nSection § 2 — résumé.\nEND\n".encode(),
         "scripts/run.py": b"#!/usr/bin/env python3\nprint('ok')\n"}
SOURCE = {"text-skill": {"SKILL.md": TEXT_MD}, "multi-skill": MULTI}     # source slugs 2000000, 2000001


def packer_results(skills, bundle="migrated-skills", executables=None, with_report=True):
    """The result files of a Skill Packer run that packed `skills` ({name: {path: bytes}})."""
    files, packed, sums = {}, [], []
    for i, (name, content) in enumerate(skills.items()):
        paths = [f"{bundle}/skills/{name}/{rel}" for rel in sorted(content)]
        for rel, data in content.items():
            files[f"{bundle}/skills/{name}/{rel}"] = data
            sums.append(f"{hashlib.sha256(data).hexdigest()}  skills/{name}/{rel}\n")
        entry = {"name": name, "slug": f"200000{i}", "path": "/", "commit": "abc1234", "files": len(content),
                 "bytes": sum(map(len, content.values())), "paths": paths}
        if executables is not None:
            entry["executables"] = executables.get(name, [])
        packed.append(entry)
    files[f"{bundle}/SHA256SUMS"] = "".join(sorted(sums, key=lambda line: line.split("  ", 1)[1])).encode()
    if with_report:
        files["report.json"] = json.dumps({"source": "https://source.example.com", "bundle": bundle,
                                           "packed": packed, "skipped": []}).encode()
    return files


class FakeTarget:
    """The target's API for one finished packer run, with its results in memory."""
    def __init__(self, host, results, state="completed"):
        self.host, self.results, self.state, self.downloads = host, results, state, []

    def get_computation(self, cid):
        return {"id": cid, "state": self.state}

    def result_bytes(self, cid, path):
        self.downloads.append(path)
        return self.results.get(path)

    def result_text(self, cid, path):
        data = self.result_bytes(cid, path)
        return None if data is None else data.decode()

    def list_results(self, cid, path=""):
        prefix, items = (path + "/" if path else ""), {}
        for p in self.results:
            if p.startswith(prefix):
                head, _, rest = p[len(prefix):].partition("/")
                items[head] = {"name": head, "path": prefix + head, "type": "folder" if rest else "file"}
        return list(items.values())

    def owner_email(self):
        return "me@example.com"


def copy_repo(base: Path, slug: str, files: dict, executable=(), links=None):
    """base/capsule-<slug>.git: a new skill on the target whose one commit holds `files` (and `links`)."""
    work = base / f"work-{slug}"
    for rel, data in files.items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_bytes(data)
    for rel, target in (links or {}).items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).symlink_to(target)
    git = lambda *a: subprocess.run(["git", *a], cwd=work, check=True, capture_output=True)  # noqa: E731
    git("init", "-q")
    git("add", "-A")
    for rel in executable:
        git("update-index", "--chmod=+x", rel)
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "copy")
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(base / f"capsule-{slug}.git")], check=True,
                   capture_output=True)


def by_path(skill: dict) -> dict:
    return {f["path"]: f for f in skill["files"]}


def test_parse_sums_and_expected_skills():
    a, b = "a" * 64, "B" * 64
    sums = sb.parse_sums(f"{a}  skills/one/SKILL.md\n{b} *skills/one/bin/run.sh\r\njunk\n{a}  README.md\n\n"
                         f"\\{a}  skills/one/back\\\\slash\\nnew.txt\n")
    assert sums == {"one": {"SKILL.md": a, "bin/run.sh": b.lower(), "back\\slash\nnew.txt": a}}
    sums["one"].pop("back\\slash\nnew.txt")
    report = {"packed": [{"name": "one", "slug": "1234567", "paths": ["mb/skills/one/SKILL.md", "mb/skills/one/ln"],
                          "executables": ["mb/skills/one/bin/run.sh", "skills/one/x.sh", "y.sh"]}, "junk", {"name": ""}]}
    exp = sb.expected_skills(sums, report, "mb")
    assert exp["one"]["executables"] == {"bin/run.sh", "x.sh", "y.sh"} and exp["one"]["listed"] == {"SKILL.md", "ln"}
    assert exp["one"]["slug"] == "1234567" and sb.expected_skills(sums, None, "mb")["one"]["executables"] is None


def test_mismatch_detail_shows_where_the_copy_differs():
    original, copy = "line one\nmoved — here\n".encode(), "line one\nmoved â€” here\n".encode()   # mojibake
    at = original.index("—".encode())
    assert sb.first_difference(original, copy) == at and sb.first_difference(b"abc", b"abcdef") == 3
    assert sb.mismatch_detail(original, copy) == (
        f'first difference at byte {at}: original "line one\\nmoved — here\\n", copy "line one\\nmoved â€” here\\n"; '
        f"{len(original)} bytes in the original, {len(copy)} in the copy")
    long_a, long_b = ("—" * 10 + "abZ").encode(), ("—" * 10 + "abY").encode()       # starts on a character
    assert sb.mismatch_detail(long_a, long_b) == (
        'first difference at byte 32: original "…' + "—" * 8 + 'abZ", copy "…' + "—" * 8 + 'abY"')
    assert sb.mismatch_detail(None, b"xyz") == "checksum differs (3 bytes in the copy)"


def test_verify_copies_all_match_and_a_script_lost_its_exec_bit(tmp_path):
    copy_repo(tmp_path, "7000001", SOURCE["text-skill"])
    copy_repo(tmp_path, "7000002", MULTI)                              # scripts/run.py arrives as 100644
    target = FakeTarget(f"file://{tmp_path}", packer_results(SOURCE))
    res = sb.verify_copies(target, f"Computation ID: `{CID}`", ["7000001", "[7000002](/capsule/7000002)"], "", "tok")
    assert res["ok"] and res["bundle"] == "migrated-skills" and not res["not_found"] and not res["executables_listed"]
    assert res["counts"] == {"OK": 4, "MISMATCH": 0, "MISSING": 0, "EXTRA": 0}
    assert [(s["slug"], s["matched"], list(by_path(s))) for s in res["skills"]] == [
        ("7000001", "text-skill", ["SKILL.md"]),
        ("7000002", "multi-skill", ["SKILL.md", "references/guide.md", "scripts/run.py"])]
    assert res["exec"] == [{"slug": "7000002", "path": "scripts/run.py", "repo_path": "scripts/run.py",
                            "certain": False}]
    assert target.downloads == ["report.json", "migrated-skills/SHA256SUMS"]   # originals only for mismatches
    assert sb.verify_verdict(res) == ("success", "Every file matches its packed original (4 files in 2 skills).")
    lines = sb.format_verify(res)
    assert lines[0] == f"Computation {CID} on file://{tmp_path}: 2 packed skills in migrated-skills/"
    assert "  OK          scripts/run.py  (starts with #! but isn't executable in the copy)" in lines
    assert "Summary: 4 OK, 0 MISMATCH, 0 MISSING, 0 EXTRA in 2 skills." in lines
    assert f"  git clone file://{tmp_path}/capsule-7000002.git && cd capsule-7000002" in lines
    assert "  git update-index --chmod=+x scripts/run.py" in lines


def test_verify_copies_negative_case(tmp_path):
    damaged = TEXT_MD.replace("—".encode(), b"-")                     # one character retyped
    copy_repo(tmp_path, "7000001", {"SKILL.md": damaged})
    copy_repo(tmp_path, "7000002", {"SKILL.md": MULTI["SKILL.md"], "scripts/run.py": MULTI["scripts/run.py"],
                                    "notes.txt": b"added\n"})
    target = FakeTarget(f"file://{tmp_path}", packer_results(SOURCE, executables={"multi-skill": ["scripts/run.py"]}))
    res = sb.verify_copies(target, CID, ["7000001", "7000002"], "me@example.com", "tok")
    assert not res["ok"] and res["executables_listed"]
    assert res["counts"] == {"OK": 2, "MISMATCH": 1, "MISSING": 1, "EXTRA": 1}
    text, multi = (by_path(s) for s in res["skills"])
    detail = text["SKILL.md"]["detail"]
    assert text["SKILL.md"]["status"] == "MISMATCH"
    assert detail.startswith(f"first difference at byte {TEXT_MD.index('—'.encode())}: original \"…")
    assert "— then stop" in detail and "- then stop" in detail
    assert detail.endswith(f"; {len(TEXT_MD)} bytes in the original, {len(damaged)} in the copy")
    assert target.downloads[-1] == "migrated-skills/skills/text-skill/SKILL.md"   # the original, for the snippet
    assert (multi["references/guide.md"]["status"], multi["notes.txt"]["status"]) == ("MISSING", "EXTRA")
    assert multi["scripts/run.py"]["status"] == "OK" and multi["scripts/run.py"]["exec"] == "lost"
    assert [i["certain"] for i in res["exec"]] == [True]
    level, verdict = sb.verify_verdict(res)
    assert level == "error" and verdict.startswith("The copies don't match the packed originals: 1 file differs, "
                                                   "1 file is missing, 1 extra file.")
    assert "1 file was executable in the source but is not in the copy" in sb.verify_exec_note(res)


def test_verify_copies_unknown_names_clone_failures_and_unchecked_skills(tmp_path):
    copy_repo(tmp_path, "7000003", {"SKILL.md": b"---\nname: other-skill\ndescription: x\n---\n"})
    copy_repo(tmp_path, "7000004", {"SKILL.md": TEXT_MD.replace(b"name: text-skill", b"name: text-skil")})
    target = FakeTarget(f"file://{tmp_path}", packer_results(SOURCE))
    res = sb.verify_copies(target, CID, ["7000003", "7000004", "7000009", "2000001"], "me@example.com", "tok")
    other, close, absent = res["skills"]
    assert other["problems"] == ["its SKILL.md name 'other-skill' isn't one of the packed skills "
                                 "(multi-skill, text-skill)"]
    assert close["matched"] == "text-skill" and "compared with the packed skill 'text-skill'" in close["notes"][0]
    assert by_path(close)["SKILL.md"]["status"] == "MISMATCH"
    assert absent["problems"][0].startswith("clone failed:")
    assert res["notes"] == ["Left out 2000001: it is the source slug of multi-skill. Use the new skill's slug on "
                            "the target."]
    assert res["not_found"] == [{"name": "multi-skill", "slug": "2000001"}] and not res["ok"]
    rows = sb.verify_rows(res)
    assert {"Slug": "", "Skill": "multi-skill", "File": "", "Result": "NOT CHECKED",
            "Details": "not found among the slugs you gave (source slug 2000001)"} in rows
    assert "NOT CHECKED  multi-skill (source slug 2000001): not found among the slugs you gave" in sb.format_verify(res)
    assert sum(r["Result"] == "PROBLEM" for r in rows) == 2
    assert "Not checked yet: multi-skill." in sb.verify_verdict(res)[1]


def test_verify_copies_finds_the_bundle_without_report_and_never_reads_links(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-VALUE")
    copy_repo(tmp_path, "7000002", {"SKILL.md": MULTI["SKILL.md"], "scripts/run.py": MULTI["scripts/run.py"]},
              executable=["scripts/run.py"], links={"references/guide.md": str(secret), "leak": str(secret)})
    copy_repo(tmp_path, "7000005", {}, links={"SKILL.md": str(secret)})
    target = FakeTarget(f"file://{tmp_path}", packer_results({"multi-skill": MULTI}, bundle="team-skills",
                                                              with_report=False))
    res = sb.verify_copies(target, CID, ["7000002", "7000005"], "", "tok")
    assert res["bundle"] == "team-skills" and not res["executables_listed"]
    files = by_path(res["skills"][0])
    assert (files["references/guide.md"]["status"], files["references/guide.md"]["detail"]) == (
        "MISMATCH", "a symbolic link, not a file")
    assert (files["leak"]["status"], files["leak"]["detail"]) == ("EXTRA", "a symbolic link")
    assert files["scripts/run.py"]["status"] == "OK" and files["scripts/run.py"]["exec"] is None
    assert res["skills"][1]["problems"] == ["SKILL.md is a symbolic link, not a file"]
    assert "TOP-SECRET-VALUE" not in json.dumps(res) + "\n".join(sb.format_verify(res))


def test_verify_copies_input_errors():
    target = FakeTarget("file:///nowhere", packer_results(SOURCE))
    with pytest.raises(sb.BridgeError, match="computation ID"):
        sb.verify_copies(target, "comp-42", ["7000001"], "", "tok")
    with pytest.raises(sb.BridgeError, match="slugs or URLs"):
        sb.verify_copies(target, CID, ["abc", "12345678"], "", "tok")
    with pytest.raises(sb.BridgeError, match="source slug of text-skill"):
        sb.verify_copies(target, CID, ["2000000"], "", "tok")
    with pytest.raises(sb.BridgeError, match="no SHA256SUMS"):
        sb.verify_copies(FakeTarget("file:///nowhere", {}), CID, ["7000001"], "", "tok")
    target.state = "running"
    with pytest.raises(sb.BridgeError, match="hasn't finished"):
        sb.verify_copies(target, CID, ["7000001"], "", "tok")


def test_clone_timeouts_with_bytes_stderr_are_reported(monkeypatch):
    def slow(args, env, cwd=None, timeout=180, raw=False, stdin=None):
        raise subprocess.TimeoutExpired(["git", *args], timeout, output=b"", stderr=b"remote: still counting\n")
    monkeypatch.setattr(sb, "_git", slow)
    row = sb.enrich("https://source.example.com", "1234567", "me@example.com", "not-a-real-token")
    assert row["problems"] == ["clone failed: timed out (remote: still counting)"]
    res = sb.verify_copies(FakeTarget("https://target.example.com", packer_results(SOURCE)), CID, ["7000001"],
                           "me@example.com", "not-a-real-token")
    assert res["skills"][0]["problems"] == ["clone failed: timed out (remote: still counting)"] and not res["ok"]


def cli_target(monkeypatch, target):
    """Make the CLI's verify run against `target`; returns what the CLI passed in."""
    seen, real = {}, sb.verify_copies

    def verify(client, cid, slugs, git_user, token, bundle_hint=None):
        seen.update(host=client.host, token=token, slugs=slugs, git_user=git_user)
        return real(target, cid, slugs, git_user, token, bundle_hint)
    monkeypatch.setattr(sb, "verify_copies", verify)
    return seen


def test_cli_verify_asks_for_the_token_and_exits_0_when_only_an_exec_bit_differs(tmp_path, monkeypatch, capsys):
    copy_repo(tmp_path, "7000001", SOURCE["text-skill"])
    copy_repo(tmp_path, "7000002", MULTI)
    seen = cli_target(monkeypatch, FakeTarget(f"file://{tmp_path}", packer_results(SOURCE)))
    monkeypatch.delenv("DST_CO_TOKEN", raising=False)
    monkeypatch.setenv("DST_HOST", "https://target.example.com")
    prompts = []
    monkeypatch.setattr(sb.getpass, "getpass", lambda prompt="": prompts.append(prompt) or "typed-token-value")
    code = sb._cli(["verify", "--computation", CID, "--skills", "7000001,",
                    "https://target.example.com/capsule/7000002/tree", "https://source.example.com/capsule/7000005"])
    out, err = capsys.readouterr()
    assert code == 0 and prompts == ["API token for https://target.example.com (not shown): "]
    assert seen == {"host": "https://target.example.com", "token": "typed-token-value",
                    "slugs": ["7000001", "7000002"], "git_user": ""}
    assert "left out https://source.example.com/capsule/7000005" in err
    assert "typed-token-value" not in out + err
    assert "  OK          references/guide.md" in out.splitlines()
    assert "Every file matches its packed original (4 files in 2 skills)." in out
    assert "git update-index --chmod=+x scripts/run.py" in out


def test_cli_verify_exits_1_on_a_mismatch(tmp_path, monkeypatch, capsys):
    copy_repo(tmp_path, "7000001", {"SKILL.md": TEXT_MD.replace("Café".encode(), b"Cafe")})
    cli_target(monkeypatch, FakeTarget(f"file://{tmp_path}", packer_results({"text-skill": {"SKILL.md": TEXT_MD}})))
    monkeypatch.setenv("DST_CO_TOKEN", "env-token-value")
    monkeypatch.setattr(sb.getpass, "getpass", lambda prompt="": pytest.fail("asked for a token that is set"))
    code = sb._cli(["verify", "--dst-host", "target.example.com", "--computation", CID, "--skills", "7000001"])
    out = capsys.readouterr().out
    assert code == 1 and "  MISMATCH    SKILL.md  (first difference at byte" in out
    assert "The copies don't match the packed originals: 1 file differs." in out and "env-token-value" not in out


def test_committed_files_reads_blobs_and_reports_missing_objects(tmp_path):
    copy_repo(tmp_path, "7000001", {"SKILL.md": b"one\r\n", "run.sh": b"#!/bin/sh\n"}, executable=["run.sh"])
    work, env = tmp_path / "work-7000001", sb._git_env("me@example.com", "not-a-real-token", "")
    assert sb.committed_files(work, env) == {"SKILL.md": {"mode": "100644", "data": b"one\r\n"},
                                             "run.sh": {"mode": "100755", "data": b"#!/bin/sh\n"}}
    oid = subprocess.run(["git", "rev-parse", "HEAD:SKILL.md"], cwd=work, check=True, capture_output=True,
                         text=True).stdout.strip()
    (work / ".git" / "objects" / oid[:2] / oid[2:]).unlink()
    with pytest.raises(ValueError, match="1 file"):
        sb.committed_files(work, env)
