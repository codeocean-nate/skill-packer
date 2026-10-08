# Skill Packer

A Code Ocean capsule that copies Aqua custom skills from one Code Ocean deployment to another, using only the two deployments' own Git. Each user copies their own skills with their own read-only token. An optional Streamlit app in the same capsule, **Skill Bridge**, lists your skills, builds the Aqua prompt for the ones you tick, and checks the copies.

```
source deployment            this capsule (destination)              destination Aqua
My Skills ── git clone ──▶  /results/migrated-skills/  ── reads ──▶  one new skill per folder
(your skills)  (your token)   .claude-plugin/plugin.json               (owned by you)
                              skills/<name>/SKILL.md …
                              README.md, CHANGELOG.md, SHA256SUMS
```

1. On the source deployment, get the slugs of the skills you want. Either copy them from **My Skills**, or ask source Aqua: “Activate every skill in your available-skills list with your skills tool, one call per skill. For each, report the UUID of the capsule its SKILL.md was loaded from (or 'none' if it was not loaded from a capsule). Then call get_capsule on each such UUID and report the capsule's name and slug as a JSON array. Do not create, change or delete anything.” It lists your enabled skills. The Skill Bridge app does this for you.
2. Check **My Skills** on the destination for skills with the same names, and archive or rename any you're replacing.
3. Send this to Aqua on the destination, with this capsule's UUID and your slugs. Aqua runs the capsule, which clones each skill over Git and writes them in Claude plugin layout with a checksum for every file. Then Aqua creates one skill per packed folder. The Skill Bridge app builds this prompt for you.

   > Run the Skill Packer capsule &lt;capsule UUID&gt; with skills = &lt;slugs&gt;. When it finishes, read report.json from the run's results. Before creating anything, compare each name in its "packed" list with the custom skills I already have, and skip any packed skill whose name matches one of mine. For each remaining skill, create a separate stand-alone skill whose files are exact byte-for-byte copies of the files listed in that skill's "paths", keeping each file's path relative to its skill folder. Do not edit, reformat or improve anything. Commit each skill. Then list each new skill's name and slug, the skills you skipped because I already have them, the computation ID, and anything the packer skipped with its reason.

   Skills you've disabled on the destination aren't checked, so look in **My Skills** if you've disabled any.
4. Check the copies with the Skill Bridge app or against `SHA256SUMS`.

## Set it up on a Code Ocean deployment

1. **Create the capsule from this repository.** In Aqua: “Create a new capsule named skill-packer by copying this Git repository: https://github.com/codeocean-nate/skill-packer. Use the repository as the starting point, but create an independent capsule that is not linked to the Git repository. Tell me its slug and UUID.” Or use **Copy from Git** in the Create menu. Everything comes across: code, environment, App Panel and an empty `SRC_CO_TOKEN` secret slot.
2. **Attach your source token right away.** Create an API token on the source deployment (Capsule read scope), save it as a secret on this deployment, and attach it to the `SRC_CO_TOKEN` slot (Environment → Secrets), then commit. Do this before building or running it.
3. **Point it at your source deployment.** Set the `SRC_HOST` environment variable to `https://<source-host>`, or set a default for the `source_host` App Panel parameter.
4. **If the source uses an internal CA,** set `EXTRA_CA_CERT_URLS` to the CA certificates' URLs, separated by spaces, and rebuild the environment. `environment/postInstall` installs them and logs each certificate's subject.
5. **Check the network path:** run it once on one of your own skills, as a reproducible run. The output starts with `Skill Packer 2026.10.08`. See `code/README.md` for what each failure means.
6. **Turn on the Skill Bridge app** if you want it (optional, see below).
7. **Share or release it.** Each user attaches their own source token to the slot on their first run.

### Environment variables

| Variable | What to set |
|---|---|
| `SRC_HOST` | The source deployment, `https://<source-host>`. Users can override it with the `source_host` parameter |
| `EXTRA_CA_CERT_URLS` | Space-separated URLs of CA certificates (PEM or DER) for a source that uses an internal CA. Leave empty for a public CA |
| `EXTRA_CA_ALLOW_INSECURE` | Set to `1` only if a CA certificate URL can't be downloaded with verification; the build log says so. Leave it unset otherwise |
| `EXTRA_PIP_PACKAGES` | Space-separated pip packages to install at build time, e.g. `streamlit` for the Skill Bridge app. Leave empty for the packer alone |
| `PACKER_CAPSULE_ID` | This capsule's UUID, for the Skill Bridge app |

Rebuild the environment after changing any of them. When you ask Aqua to change one, list all of them with their values and say “keeping everything else unchanged”.

## Update an existing capsule

Send this to Aqua on that deployment, with your capsule's UUID:

```text
In capsule <capsule UUID>, update the Skill Packer code to the latest version from GitHub. Start a cloud workstation and run exactly this in its terminal:
set -e; rm -rf /tmp/sp; git clone -q --depth 1 https://github.com/codeocean-nate/skill-packer.git /tmp/sp; cp /tmp/sp/code/run /tmp/sp/code/skill_packer.py /code/; chmod +x /code/run; git -C /tmp/sp log -1 --format=%h
Do not change code/README.md, the environment, the secrets or any other file. Make the App Panel parameter git_user optional if it is required, and change nothing else in the App Panel. Then shut down the cloud workstation you started (a full shutdown, not hold), and only after that commit all changes with the message 'Update Skill Packer from GitHub'. Tell me the GitHub commit you updated to.
```

Send it when the capsule has no uncommitted changes, because it commits everything. It updates `code/run` and `code/skill_packer.py`, and keeps `code/README.md`, the environment, the CA certificates and the secrets as they are. The next run's output starts with `Skill Packer 2026.10.08` or a later version.

## Turn on the Skill Bridge app (optional)

The app runs in a Streamlit cloud workstation on this capsule and uses the source token users already attach for the packer. Send this to Aqua, with your capsule's UUID (twice), your `SRC_HOST`, and your current `EXTRA_CA_CERT_URLS` value or (empty):

```text
In capsule <capsule UUID>, start a cloud workstation and run exactly this in its terminal:
set -e; rm -rf /tmp/sp; git clone -q --depth 1 https://github.com/codeocean-nate/skill-packer.git /tmp/sp; cp /tmp/sp/code/run /tmp/sp/code/skill_packer.py /tmp/sp/code/app.py /tmp/sp/code/skill_bridge.py /tmp/sp/code/streamlit_app.py /code/; mkdir -p /code/.streamlit; cp /tmp/sp/code/.streamlit/config.toml /code/.streamlit/; cp /tmp/sp/environment/postInstall /root/capsule/environment/postInstall; chmod +x /code/run /root/capsule/environment/postInstall; git -C /tmp/sp log -1 --format=%h
Then shut down the cloud workstation (a full shutdown, not hold). After that, set up the environment: base image codeocean/ubuntu:22.04; apt packages ca-certificates, curl, git, openssl, python3, python3-pip and python3-yaml; run the post-install script environment/postInstall; environment variables SRC_HOST = <source URL>, EXTRA_CA_CERT_URLS = <current value, or (empty)>, EXTRA_PIP_PACKAGES = streamlit and PACKER_CAPSULE_ID = <capsule UUID>, keeping everything else unchanged. Do not change code/README.md, the App Panel or the secrets. Build the environment, show me the post-install part of the build log, and check that the streamlit command exists. Then shut down the cloud workstation you started (a full shutdown, not hold), and only after that commit all changes.
```

It also brings the packer code up to date. Send it when the capsule has no uncommitted changes, because it commits everything. It takes about 3 minutes, including the environment build.

To open the app, go to the capsule page and, under **or launch a cloud workstation**, choose the **Streamlit** icon. The app opens in the workstation. When you're done, choose **Shut down** (not **Hold**).

If the environment build fails at the pip step, set `EXTRA_PIP_PACKAGES` back to empty, listing all four variables with their values, and rebuild. The packer keeps working without the app.

Full usage, parameters, skip reasons, the Aqua prompts, the app and the copy check are in [`code/README.md`](code/README.md).

## Files

| Path | Purpose |
|---|---|
| `code/run` | Entry point |
| `code/skill_packer.py` | Clones, filters by tag, packs, writes checksums and `report.json`, and prints its version first (Python 3.9+, PyYAML) |
| `code/README.md` | User guide shown in the capsule |
| `code/app.py`, `code/streamlit_app.py` | The Skill Bridge app. A Streamlit cloud workstation serves `streamlit_app.py`, which runs `app.py` |
| `code/skill_bridge.py` | The app's logic, also usable from a terminal; `verify` checks the copies |
| `code/.streamlit/config.toml` | Streamlit settings for the app |
| `code/requirements.txt`, `code/tests/` | pip packages and offline tests for running the app outside Code Ocean |
| `environment/postInstall` | Installs extra CA certificates from `EXTRA_CA_CERT_URLS` and pip packages from `EXTRA_PIP_PACKAGES`; skips each when its variable is empty |
| `.codeocean/environment.json` | `codeocean/ubuntu:22.04` with ca-certificates, curl, git, openssl, python3, python3-pip, python3-yaml; env vars `SRC_HOST`, `EXTRA_CA_CERT_URLS`, `EXTRA_PIP_PACKAGES` |
| `.codeocean/app-panel.json` | Named parameters: `skills` (required), `git_user` (looked up from the token when empty), `source_host`, `keyword`, `bundle`, `token_env` |
| `.codeocean/secrets.json` | An empty secret slot, `SRC_CO_TOKEN`. No values |

## Good to know

- Text files copy as-is. If a skill includes binary files, check them after copying. Check every copy with the Skill Bridge app or against `SHA256SUMS`, which has a checksum for every packed file.
- The files in the new skills are regular files. If a skill needs a script to be executable, set it again in a clone of the new skill with `git update-index --chmod=+x <file>`, then commit and push. `report.json` lists the files that were executable on the source.
- The packer copies regular files and folders only. It lists any symbolic links or unusable file names it leaves out.
- Commit skills on the source before copying them.
- Copies belong to the user who runs the migration. Share them as needed on the destination.
- Skills are instructions Aqua follows. Only copy skills you trust.
