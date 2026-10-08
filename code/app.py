"""Skill Bridge: pick your Aqua skills on a source Code Ocean deployment and copy them to a target one.

Run with:  streamlit run app.py
Configuration (environment variables or capsule secrets): SRC_HOST, SRC_CO_TOKEN, DST_HOST, DST_CO_TOKEN,
PACKER_CAPSULE_ID. The sidebar can override the hosts and the env var names. Token values are never shown.
"""
from __future__ import annotations

import hashlib
import re

import pandas as pd
import streamlit as st

import skill_bridge as sb

st.set_page_config(page_title="Skill Bridge", layout="wide")
ss = st.session_state
for key, default in {"rows": {}, "order": [], "sel": {}, "src_host_loaded": "", "src_listing": None,
                     "tgt": None, "editor_ver": 0, "run": None, "git_users": {}, "flash": [], "verify": None}.items():
    ss.setdefault(key, default)

ROUGHLY = "This usually takes about 30 seconds."


def reset_all():
    for key in ("rows", "order", "sel", "src_host_loaded", "src_listing", "tgt", "run", "git_users", "flash",
                "verify"):
        ss.pop(key, None)
    ss.editor_ver = ss.get("editor_ver", 0) + 1


def host_or_error(value: str, label: str) -> str:
    try:
        return sb.normalize_host(value)
    except sb.BridgeError as e:
        st.sidebar.error(f"{label}: {e}")
        return ""


def token_note(env_name: str) -> str:
    name = (env_name or "").strip()
    if not name:
        return "No env var name set."
    return f"Token found in `{name}`." if sb.Config.token(name) else f"`{name}` is empty or not set."


def size(n: int) -> str:
    return f"{n / 1024:.1f} KB" if n else ""


# ---------------------------------------------------------------------------------------------------------------
# Sidebar: settings

cfg = sb.Config.from_env()
def this_deployment() -> str:
    # The workstation serves this app from the target deployment itself; the browser's Origin header names it.
    try:
        origin = st.context.headers.get("Origin") or ""
    except Exception:
        return ""
    return origin.rstrip("/") if re.fullmatch(r"https://[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+", origin.rstrip("/")) else ""


with st.sidebar:
    st.header("Settings")
    src_host = host_or_error(st.text_input("Source deployment URL", value=cfg.src_host, key="src_host_in",
                                           help="Where your skills are now, e.g. https://codeocean.dev.example.com"),
                             "Source")
    src_env = st.text_input("Source token env var", value=cfg.src_token_env, key="src_env",
                            help="The environment variable (or capsule secret) that holds your source API token.")
    st.caption(token_note(src_env))
    dst_host = host_or_error(st.text_input("Target deployment URL", value=cfg.dst_host or this_deployment(), key="dst_host_in",
                                           help="Where the copies go."), "Target")
    dst_env = st.text_input("Target token env var", value=cfg.dst_token_env, key="dst_env",
                            help="Optional. With a target token the app can check which skills the target already "
                                 "has and run the Skill Packer for you.")
    st.caption(token_note(dst_env))
    packer_id = st.text_input("Skill Packer capsule ID (on the target)", value=cfg.packer_id, key="packer_id",
                              help="The UUID of the Skill Packer capsule on the target deployment.").strip()
    with st.expander("Advanced"):
        git_user_in = st.text_input("Source Git username (your email)", key="git_user_in",
                                    help="Leave empty: the app looks up the source token owner's email.").strip()
        dst_git_user_in = st.text_input("Target Git username (your email)", key="dst_git_user_in",
                                        help="Used to check the copies. Leave empty: the app looks up the target "
                                             "token owner's email.").strip()
        token_env_param = st.text_input("Packer token_env parameter", key="token_env_param",
                                        help="The env var name your source-token secret is attached as in the "
                                             "Skill Packer capsule. Leave empty to use the packer's default.").strip()
    st.button("Clear loaded results", key="clear", on_click=reset_all)

src_tok, dst_tok = sb.Config.token(src_env), sb.Config.token(dst_env)

# Results loaded for another source deployment don't apply to this one.
if ss.src_host_loaded and ss.src_host_loaded != src_host:
    ss.rows, ss.order, ss.sel, ss.src_listing, ss.run = {}, [], {}, None, None
    ss.src_host_loaded = ""
    ss.editor_ver += 1
if ss.tgt and ss.tgt["host"] != dst_host:
    ss.tgt = None
    for r in ss.rows.values():
        r["on_target"] = []
if ss.verify and ss.verify["host"] != dst_host:
    ss.verify = None


def git_user() -> str:
    if git_user_in:
        return git_user_in
    if src_host not in ss.git_users:
        ss.git_users[src_host] = sb.CoClient(src_host, src_tok, "source").owner_email()
    return ss.git_users[src_host]


def target_names() -> set[str]:
    return ss.tgt["listing"].names() if ss.tgt else set()


def add_rows(rows: list[dict], replace_origin: str | None = None) -> None:
    """Store enriched rows (keyed by slug) and give each its default selection."""
    if replace_origin:
        for slug in [s for s in ss.order if ss.rows[s]["origin"] == replace_origin]:
            ss.order.remove(slug)
            ss.rows.pop(slug)
            ss.sel.pop(slug, None)
    sb.mark_on_target(rows, target_names())
    for r in rows:
        if r["slug"] not in ss.rows:
            ss.order.append(r["slug"])
        ss.rows[r["slug"]] = r
        ss.sel[r["slug"]] = sb.default_selected(r)
    ss.editor_ver += 1


# ---------------------------------------------------------------------------------------------------------------
# 1. Load skills

st.title("Skill Bridge")
st.write("Copy your Aqua skills from one Code Ocean deployment to another. Load your skills, tick the ones to copy, "
         "and send the prompt below to Aqua on the target (or run the Skill Packer from here).")

st.subheader("1. Load your skills")
c1, c2 = st.columns(2)
with c1:
    load_src = st.button("Load source skills", key="load_src", type="primary", disabled=not (src_host and src_tok))
    if not src_host:
        st.caption("Set the source deployment URL in Settings.")
    elif not src_tok:
        st.caption(f"Set your source API token in `{src_env or 'SRC_CO_TOKEN'}`.")
    else:
        st.caption(f"Aqua on {src_host} lists the skills that are enabled for you.")
with c2:
    load_tgt = st.button("Load target skills", key="load_tgt", disabled=not (dst_host and dst_tok))
    if not (dst_host and dst_tok):
        st.caption("Optional: set a target URL and token to see which skills the target already has.")
    else:
        st.caption(f"Skills whose name already exists on {dst_host} start unticked.")

if load_src:
    try:
        with st.spinner(f"Asking Aqua on {src_host} for your enabled skills. {ROUGHLY}"):
            listing = sb.list_skills(src_host, src_tok, "source")
        ss.src_listing = listing
        with st.spinner(f"Reading {len(listing.skills)} skills over Git…"):
            user = git_user()
            rows = sb.enrich_many(src_host, [{"slug": s["slug"], "uuid": s["uuid"], "capsule_name": s["capsule_name"]}
                                             for s in listing.skills], user, src_tok)
        add_rows(rows, replace_origin="aqua")
        ss.src_host_loaded = src_host
        ss.flash.append(("success", f"Aqua found {len(listing.skills)} custom skills in {listing.seconds:.0f} s."
                         if listing.skills else "Aqua found no custom skills that are enabled for you. Add skills "
                                                "by slug below."))
    except sb.BridgeError as e:
        st.error(str(e))

if load_tgt:
    try:
        with st.spinner(f"Asking Aqua on {dst_host} for your enabled skills. {ROUGHLY}"):
            tl = sb.list_skills(dst_host, dst_tok, "target")
        ss.tgt = {"host": dst_host, "listing": tl}
        rows = [ss.rows[s] for s in ss.order]
        sb.mark_on_target(rows, tl.names())
        for r in rows:
            if r["on_target"]:
                ss.sel[r["slug"]] = False
        ss.editor_ver += 1
        clash = sum(1 for r in rows if r["on_target"])
        ss.flash.append(("success", f"The target has {len(tl.skills)} custom skills (found in {tl.seconds:.0f} s)."
                         + (f" {clash} of your source skills are already there." if rows else "")))
    except sb.BridgeError as e:
        st.error(str(e))

for kind, msg in ss.flash:
    getattr(st, kind)(msg)
ss.flash = []

with st.expander("Add skills by slug or URL"):
    st.caption("To include a skill that isn't in the list (for example one that is disabled), paste its slug or its "
               "URL from My Skills on the source.")
    extra = st.text_area("Slugs or URLs", key="extra_slugs", placeholder="1234567, https://<source>/capsule/7654321/tree")
    if st.button("Add these skills", key="add_slugs", disabled=not (src_host and src_tok)):
        slugs, wrong = sb.slugs_on_host(extra, src_host)
        slugs = [s for s in slugs if s not in ss.rows]
        for w in wrong:
            st.warning(f"{w} is not on the source deployment {src_host}; slugs differ between deployments.")
        if slugs:
            try:
                with st.spinner(f"Reading {len(slugs)} skills over Git…"):
                    add_rows(sb.enrich_many(src_host, [{"slug": s, "origin": "manual"} for s in slugs],
                                            git_user(), src_tok))
                ss.src_host_loaded = src_host
            except sb.BridgeError as e:
                st.error(str(e))
        elif not wrong:
            st.info("No new 7-digit slugs found.")

if ss.src_listing or ss.tgt:
    with st.expander("Details from Aqua"):
        for label, lst in (("Source", ss.src_listing), ("Target", ss.tgt and ss.tgt["listing"])):
            if not lst:
                continue
            st.markdown(f"**{label}** · session `{lst.session_id}` · {lst.seconds:.0f} s")
            if label == "Target" and lst.skills:
                st.dataframe(pd.DataFrame([{"Skill": s["name"], "Capsule": s["capsule_name"], "Slug": s["slug"]}
                                           for s in lst.skills]), hide_index=True)
            for n in lst.notes:
                st.caption(n)
            st.code(lst.reply or "(empty reply)", language="markdown", wrap_lines=True)

# ---------------------------------------------------------------------------------------------------------------
# 2. Choose

st.subheader("2. Choose skills to copy")
rows = [ss.rows[s] for s in ss.order]
selected: list[str] = []
if not rows:
    st.info("Load your source skills, or add skills by slug, to see them here.")
else:
    all_tags = sorted({t for r in rows for t in r["tags"]}, key=str.casefold)
    f1, f2, f3 = st.columns([4, 1, 1], vertical_alignment="bottom")
    tag_filter = f1.multiselect("Show only skills tagged", all_tags, key="tag_filter")
    wanted = {t.casefold() for t in tag_filter}
    visible = [r for r in rows if not wanted or wanted & {t.casefold() for t in r["tags"]}]
    if f2.button("Select all", key="select_all"):
        for r in visible:
            ss.sel[r["slug"]] = True
        ss.editor_ver += 1
    if f3.button("Select none", key="select_none"):
        for r in visible:
            ss.sel[r["slug"]] = False
        ss.editor_ver += 1

    df = pd.DataFrame([{
        "Copy": bool(ss.sel.get(r["slug"])),
        "Skill": ", ".join(r["names"]) or r.get("capsule_name") or "",
        "Slug": r["slug"],
        "Description": r["description"],
        "Tags": ", ".join(r["tags"]),
        "Commit": r["commit"],
        "Files": r["files"] or None,
        "Size": size(r["bytes"]),
        "Status": sb.row_status(r),
    } for r in visible])
    view = hashlib.sha1(" ".join(r["slug"] for r in visible).encode()).hexdigest()[:8]
    edited = st.data_editor(
        df, key=f"editor-{ss.editor_ver}-{view}", hide_index=True,
        disabled=[c for c in df.columns if c != "Copy"],
        column_config={
            "Copy": st.column_config.CheckboxColumn("Copy", help="Tick to copy this skill", width="small"),
            "Slug": st.column_config.TextColumn(width="small"),
            "Description": st.column_config.TextColumn(width="large"),
            "Commit": st.column_config.TextColumn(width="small"),
            "Files": st.column_config.NumberColumn(width="small"),
            "Status": st.column_config.TextColumn(width="medium"),
        })
    for slug, on in zip(edited["Slug"], edited["Copy"]):
        ss.sel[slug] = bool(on)
    selected = [r["slug"] for r in visible if ss.sel.get(r["slug"])]
    hidden = [s for s in ss.order if ss.sel.get(s) and s not in {r["slug"] for r in visible}]
    note = f"{len(selected)} of {len(visible)} shown skills selected."
    if hidden:
        note += f" {len(hidden)} more selected skills are hidden by the tag filter and are not included."
    st.caption(note)
    blocked = [s for s in selected if sb.is_blocked(ss.rows[s])]
    if blocked:
        st.warning("The packer will skip these selected slugs: " + "; ".join(
            f"{s} ({'; '.join(ss.rows[s]['problems'])})" for s in blocked))
    clash = [s for s in selected if ss.rows[s]["on_target"]]
    if clash:
        st.warning("These selected skills already exist on the target, so the target will end up with two skills of "
                   "the same name: " + ", ".join(", ".join(ss.rows[s]["on_target"]) for s in clash))

# ---------------------------------------------------------------------------------------------------------------
# 3. Copy

st.subheader("3. Copy them with Aqua on the target")
if not selected:
    st.info("Select at least one skill above.")
else:
    st.write(f"Start a new Aqua chat on {dst_host or 'the target deployment'} and send this prompt. Aqua runs the "
             "Skill Packer, then creates one skill per packed folder.")
    st.code(sb.build_pack_prompt(selected, packer_id), language=None, wrap_lines=True)
    if not packer_id:
        st.warning(f"Replace {sb.PACKER_PLACEHOLDER} with the Skill Packer capsule's ID on the target, or set it in "
                   "Settings.")
    st.caption("The slugs on their own (for the packer's App Panel):")
    st.code(", ".join(selected), language=None)

    st.markdown("**Or run the packer from here**")
    if not (dst_host and dst_tok and packer_id):
        st.caption("Set a target URL, a target token and the Skill Packer capsule ID in Settings to run the packer "
                   "from here. Then Aqua on the target only has to create the skills.")
    elif st.button(f"Run the packer now ({len(selected)} skill{'s' if len(selected) != 1 else ''})",
                   key="run_packer"):
        with st.status(f"Running the Skill Packer on {dst_host}…", expanded=True) as status:
            try:
                dst = sb.CoClient(dst_host, dst_tok, "target")
                panel = dst.app_panel(packer_id)
                needs_user = any(p.get("param_name") == "git_user" and p.get("required") for p in panel)
                params = sb.packer_parameters(panel, selected, src_host, git_user() if needs_user else "",
                                              token_env_param)
                st.write("Parameters: " + ", ".join(p["param_name"] for p in params))
                line = st.empty()
                res = sb.run_packer(dst, packer_id, params,
                                    on_update=lambda c: line.write(f"Computation `{c.get('id')}`: "
                                                                   f"{c.get('state')}"))
                ss.run = res
                status.update(label="Packer run finished" if res["ok"] else "Packer run finished with problems",
                              state="complete" if res["ok"] else "error", expanded=False)
            except sb.BridgeError as e:
                status.update(label="Couldn't run the packer", state="error")
                st.error(str(e))

run = ss.run
if run:
    st.markdown(f"**Packer run** `{run['id']}` · state {run['state']} · exit code {run['exit_code']}"
                + (f" · {run['run_time']} s" if run.get("run_time") is not None else ""))
    if run["ok"]:
        st.success(f"Packed {len(run['packed'])} skill(s)" + (f", skipped {len(run['skipped'])}." if run["skipped"]
                                                              else "."))
    elif run["exit_code"] not in (0, None):
        st.error(f"The packer stopped with exit code {run['exit_code']}. Its output is below.")
    else:
        st.error("The packer packed nothing. The reasons are below.")
    if run["packed"]:
        st.dataframe(pd.DataFrame([p if isinstance(p, dict) else {"name": p} for p in run["packed"]]).drop(
            columns=["paths"], errors="ignore"), hide_index=True)
    if run["skipped"]:
        st.markdown("Skipped:")
        st.dataframe(pd.DataFrame(run["skipped"]), hide_index=True)
    with st.expander("Packer output"):
        st.code(run["output"] or "(no output file)", language=None)
    if run["packed"]:
        st.write(f"Now start a new Aqua chat on {dst_host or 'the target'} and send this prompt to create the skills:")
        st.code(sb.build_unpack_prompt(run["id"], run["bundle"]), language=None, wrap_lines=True)

# ---------------------------------------------------------------------------------------------------------------
# 4. Check the copies

st.subheader("4. Check the copies")
st.write("When Aqua has created the new skills, check that every file in them matches the packed original exactly.")
if run and ss.get("verify_prefill") != run["id"]:          # a packer run started here fills in its ID once
    ss.verify_comp = run["id"]
    ss.verify_prefill = run["id"]
k1, k2 = st.columns([2, 3])
verify_comp = k1.text_input("Packer computation ID", key="verify_comp",
                            help="The ID of the Skill Packer run that packed the skills. Aqua's reply includes it.")
verify_slugs = k2.text_area("New skills' slugs or URLs", key="verify_slugs", height=80,
                            placeholder="From Aqua's reply, e.g. 1234567, https://<target>/capsule/7654321")
verify_tok = dst_tok
if not dst_tok:
    verify_tok = st.text_input("Target API token", type="password", key="verify_token",
                               help="Used for this check only and kept only for this browser session.").strip()
if not dst_host:
    st.caption("Set the target deployment URL in Settings to check the copies.")
if st.button("Check copies", key="verify_copies", disabled=not dst_host):
    ss.verify = None
    slugs, elsewhere = sb.slugs_on_host(verify_slugs, dst_host)
    for item in elsewhere:
        st.warning(f"{item} is not on the target deployment {dst_host}, so it was left out. Use the new skills' "
                   "slugs or URLs on the target.")
    if not verify_tok:
        st.error("Enter your API token for the target deployment.")
    elif not verify_comp.strip():
        st.error("Enter the computation ID of the Skill Packer run.")
    elif not slugs:
        st.error("Enter the slugs or URLs of the new skills on the target.")
    else:
        try:
            with st.spinner(f"Checking {len(slugs)} new skill{'s' if len(slugs) != 1 else ''} on {dst_host}…"):
                ss.verify = sb.verify_copies(sb.CoClient(dst_host, verify_tok, "target"), verify_comp, slugs,
                                             dst_git_user_in, verify_tok)
        except sb.BridgeError as e:
            st.error(str(e))

checked = ss.verify
if checked:
    level, verdict = sb.verify_verdict(checked)
    getattr(st, level)(verdict)
    st.dataframe(pd.DataFrame(sb.verify_rows(checked)), hide_index=True,
                 column_config={"Slug": st.column_config.TextColumn(width="small"),
                                "Result": st.column_config.TextColumn(width="small"),
                                "Details": st.column_config.TextColumn(width="large")})
    exec_note = sb.verify_exec_note(checked)
    if exec_note:
        st.warning(exec_note)
        st.code(sb.exec_fix_commands(checked), language="bash")
    st.caption(f"Checked against computation `{checked['computation']}` ({checked['bundle']}/SHA256SUMS) on "
               f"{checked['host']}.")
