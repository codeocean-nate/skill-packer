"""Headless UI tests for app.py with streamlit.testing.v1.AppTest. Network calls are faked."""
import json
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
import skill_bridge as sb  # noqa: E402  (the app imports this same module object)

FAKE_SRC_TOKEN, FAKE_DST_TOKEN = "src-token-value-0123456789", "dst-token-value-9876543210"
SRC, DST = "https://source.example.com", "https://target.example.com"


def row(slug, names, tags=(), problems=(), origin="aqua"):
    r = sb.new_row(slug, origin)
    r.update(names=list(names), tags=list(tags), problems=list(problems), commit="abc1234" if not problems else "",
             files=2, bytes=2048, description=f"{names[0] if names else slug} description")
    return r


SOURCE_ROWS = {
    "1111111": row("1111111", ["alpha"], ["genomics"]),
    "2222222": row("2222222", ["beta"], ["qc", "genomics"]),
    "3333333": row("3333333", ["gamma"], ["qc"]),
    "4444444": row("4444444", [], problems=["not committed: commit the skill on the source"]),
    "5555555": row("5555555", ["manual-one"], ["extra"], origin="manual"),
}


def listing(names_slugs, available=()):
    return sb.Listing(skills=[{"name": n, "capsule_name": n, "uuid": None, "slug": s} for n, s in names_slugs],
                      available=list(available), reply="```json\n[]\n```", seconds=27.0, session_id="sess-1")


@pytest.fixture
def fakes(monkeypatch):
    calls = {"list": [], "enrich": [], "run": []}

    def list_skills(host, token, label="source", verify=True):
        calls["list"].append((host, label))
        assert token in (FAKE_SRC_TOKEN, FAKE_DST_TOKEN)
        if label == "source":
            return listing([("alpha", "1111111"), ("beta", "2222222"), ("gamma", "3333333"), ("", "4444444")])
        return listing([("beta", "9999999")], available=["science", "beta"])

    def enrich_many(host, items, user, token, workers=6):
        calls["enrich"].append([i["slug"] for i in items])
        out = []
        for i in items:
            r = dict(SOURCE_ROWS.get(i["slug"]) or row(i["slug"], [f"skill-{i['slug']}"]))
            r.update(origin=i.get("origin", "aqua"), problems=list(r["problems"]), on_target=[])
            out.append(r)
        return out

    def run_packer(client, packer_id, params, timeout=1200, poll=5, on_update=None):
        calls["run"].append((client.host, packer_id, params))
        if on_update:
            on_update({"id": "comp-42", "state": "running"})
        return {"id": "comp-42", "state": "completed", "end_status": "succeeded", "exit_code": 0, "run_time": 4,
                "ok": True, "output": '{"packed": ["alpha"], "skipped": []}', "report": None,
                "bundle": "migrated-skills", "packed": [{"name": "alpha", "slug": "1111111", "commit": "abc1234",
                                                         "files": 2, "bytes": 2048, "paths": ["x"]}],
                "skipped": [], "messages": ""}

    monkeypatch.setattr(sb, "list_skills", list_skills)
    monkeypatch.setattr(sb, "enrich_many", enrich_many)
    monkeypatch.setattr(sb, "run_packer", run_packer)
    monkeypatch.setattr(sb.CoClient, "owner_email", lambda self: "me@example.com")
    monkeypatch.setattr(sb.CoClient, "app_panel", lambda self, cid: [
        {"param_name": "skills", "required": True}, {"param_name": "git_user", "required": True},
        {"param_name": "source_host"}, {"param_name": "token_env", "default_value": "K"}])
    return calls


@pytest.fixture
def env(monkeypatch):
    for k in ("SRC_HOST", "DST_HOST", "SRC_CO_TOKEN", "DST_CO_TOKEN", "PACKER_CAPSULE_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SRC_HOST", SRC)
    monkeypatch.setenv("SRC_CO_TOKEN", FAKE_SRC_TOKEN)
    monkeypatch.setenv("DST_HOST", DST)
    monkeypatch.setenv("DST_CO_TOKEN", FAKE_DST_TOKEN)
    monkeypatch.setenv("PACKER_CAPSULE_ID", "c0ffee00-0000-4000-8000-000000000000")


def app():
    at = AppTest.from_file(str(APP_DIR / "app.py"), default_timeout=30)
    return at.run()


def all_text(at) -> str:
    parts = []
    for kind in ("markdown", "caption", "code", "info", "success", "warning", "error", "text"):
        parts += [str(getattr(e, "value", "")) for e in getattr(at, kind)]
    parts += [str(e.value) for e in at.text_input]
    return "\n".join(parts)


def editor(at):
    return next(d.value for d in at.dataframe if "Copy" in d.value.columns)


def prompt_code(at) -> str:
    return next(c.value for c in at.code if c.value.startswith("Run the Skill Packer capsule"))


def test_renders_without_config(monkeypatch, fakes):
    for k in ("SRC_HOST", "DST_HOST", "SRC_CO_TOKEN", "DST_CO_TOKEN", "PACKER_CAPSULE_ID"):
        monkeypatch.delenv(k, raising=False)
    at = app()
    assert not at.exception
    assert at.button(key="load_src").disabled and at.button(key="load_tgt").disabled
    assert any("Load your source skills" in i.value for i in at.info)


def test_load_source_select_and_prompt(env, fakes):
    at = app()
    assert not at.button(key="load_src").disabled
    at.button(key="load_src").click().run()
    assert not at.exception
    assert fakes["list"] == [(SRC, "source")]
    assert fakes["enrich"] == [["1111111", "2222222", "3333333", "4444444"]]
    assert any("Aqua found 4 custom skills" in s.value for s in at.success)
    table = editor(at)
    assert list(table["Slug"]) == ["1111111", "2222222", "3333333", "4444444"]
    assert list(table["Copy"]) == [True, True, True, False]             # the uncommitted one starts unticked
    assert "not committed" in table.loc[3, "Status"]
    prompt = prompt_code(at)
    assert "c0ffee00-0000-4000-8000-000000000000 with skills = 1111111, 2222222, 3333333." in prompt
    assert any(c.value == "1111111, 2222222, 3333333" for c in at.code)
    assert FAKE_SRC_TOKEN not in all_text(at) and FAKE_DST_TOKEN not in all_text(at)


def test_target_marks_existing_skills(env, fakes):
    at = app()
    at.button(key="load_src").click().run()
    at.button(key="load_tgt").click().run()
    assert not at.exception
    table = editor(at)
    beta = table[table["Slug"] == "2222222"].iloc[0]
    assert beta["Copy"] == False and "already on target" in beta["Status"]  # noqa: E712
    assert "skills = 1111111, 3333333." in prompt_code(at)
    assert any("The target has 1 custom skills" in s.value for s in at.success)


def test_target_first_then_source(env, fakes):
    at = app()
    at.button(key="load_tgt").click().run()
    at.button(key="load_src").click().run()
    table = editor(at)
    assert table[table["Slug"] == "2222222"].iloc[0]["Copy"] == False  # noqa: E712


def test_tag_filter_select_all_and_none(env, fakes):
    at = app()
    at.button(key="load_src").click().run()
    at.button(key="load_tgt").click().run()
    at.multiselect(key="tag_filter").set_value(["qc"]).run()
    assert list(editor(at)["Slug"]) == ["2222222", "3333333"]
    at.button(key="select_all").click().run()
    assert "skills = 2222222, 3333333." in prompt_code(at)         # select all overrides "already on target"
    assert any("already exist on the target" in w.value for w in at.warning)
    assert any("hidden by the tag filter" in c.value for c in at.caption)
    at.button(key="select_none").click().run()
    assert any("Select at least one skill" in i.value for i in at.info)


def test_add_slugs_manually(env, fakes):
    at = app()
    at.text_area(key="extra_slugs").input(f"5555555, {SRC}/capsule/1111111/tree, https://elsewhere.example.com/capsule/7777777").run()
    at.button(key="add_slugs").click().run()
    assert not at.exception
    assert fakes["enrich"] == [["5555555", "1111111"]]
    table = editor(at)
    assert "added by slug" in table[table["Slug"] == "5555555"].iloc[0]["Status"]
    assert any("elsewhere.example.com" in w.value for w in at.warning)


def test_run_packer_now(env, fakes):
    at = app()
    at.button(key="load_src").click().run()
    at.multiselect(key="tag_filter").set_value(["genomics"]).run()
    at.button(key="select_none").click().run()
    at.multiselect(key="tag_filter").set_value([]).run()
    # "Select none" under the genomics filter unticked alpha and beta; gamma stays ticked, 4444444 starts unticked.
    at.button(key="run_packer").click().run()
    assert not at.exception
    host, packer, params = fakes["run"][0]
    assert host == DST and packer == "c0ffee00-0000-4000-8000-000000000000"
    assert params == [{"param_name": "skills", "value": "3333333"},
                      {"param_name": "source_host", "value": SRC},
                      {"param_name": "git_user", "value": "me@example.com"}]
    text = all_text(at)
    assert "comp-42" in text and "exit code 0" in text
    assert any(c.value.startswith("Computation comp-42 (a run of the Skill Packer capsule) has results under "
                                  "migrated-skills/") for c in at.code)
    assert FAKE_SRC_TOKEN not in text and FAKE_DST_TOKEN not in text


def test_errors_are_shown(env, fakes, monkeypatch):
    def boom(host, token, label="source", verify=True):
        raise sb.BridgeError("The source deployment rejected the API token (HTTP 401).")
    monkeypatch.setattr(sb, "list_skills", boom)
    at = app()
    at.button(key="load_src").click().run()
    assert not at.exception
    assert any("rejected the API token" in e.value for e in at.error)


def test_no_packer_id_uses_placeholder(env, fakes, monkeypatch):
    monkeypatch.delenv("PACKER_CAPSULE_ID")
    at = app()
    at.button(key="load_src").click().run()
    assert "Skill Packer capsule <PACKER_CAPSULE_ID> with skills" in prompt_code(at)
    assert not any(b.key == "run_packer" for b in at.button)


def test_streamlit_app_entry_point(env, fakes):
    at = AppTest.from_file(str(APP_DIR / "streamlit_app.py"), default_timeout=30).run()
    assert not at.exception and at.title[0].value == "Skill Bridge"
    at.button(key="load_src").click().run()
    assert not at.exception
    assert list(editor(at)["Slug"]) == ["1111111", "2222222", "3333333", "4444444"]
