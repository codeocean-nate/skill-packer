# Skill Packer

Copies your Aqua skills from another Code Ocean deployment (the **source**) into this one, without an external Git host. The capsule clones each skill over the source deployment's own Git and packs the skills into `/results` in Claude plugin layout. Aqua on this deployment then turns each packed folder into a separate skill that you own.

If the capsule owner has turned it on, the **Skill Bridge** app runs in a cloud workstation on this capsule. It lists your skills on the source, builds the Aqua prompt for the ones you tick, and checks the copies. See “Use the Skill Bridge app” below.

## Before your first run (once)

1. **Create a token on the source deployment.** Account → Access tokens, with **Capsule read** scope.
2. **Save it as a secret on this deployment.** Account → Secrets, Custom Key type.
3. **Attach it to this capsule.** On your first run, choose **Fix Credentials** and pick your secret for the `SRC_CO_TOKEN` slot. Every user picks their own; nobody else's secret is ever used for your run. Viewers can do this too.

   The capsule reads the token from the env var named in `token_env` (default `SRC_CO_TOKEN`).

> **Capsule owners:** commit right after attaching or changing a secret slot. The change is recorded in `.codeocean/secrets.json`, which holds only the slot name, never the token.

## 1. Find the skills to copy

On the source deployment, open **My Skills** and copy each skill's URL (`…/capsule/1234567/tree`) or just its 7-digit slug. Filter by tag first if you only want some.

- Or ask Aqua on the source deployment, using exactly this wording: “Activate every skill in your available-skills list with your skills tool, one call per skill. For each, report the UUID of the capsule its SKILL.md was loaded from (or 'none' if it was not loaded from a capsule). Then call get_capsule on each such UUID and report the capsule's name and slug as a JSON array. Do not create, change or delete anything.” It lists your enabled skills.
- If you know the skills' names, ask source Aqua for just those: “Activate each of these skills with your skills tool: &lt;name-1&gt;, &lt;name-2&gt;. For each, report the UUID of the capsule its SKILL.md was loaded from, then call get_capsule on that UUID and report the capsule's name and slug. Do not create, change or delete anything.” The skills must be enabled on the source.
- Or open the Skill Bridge app and choose **Load source skills**.
- To include a disabled skill, enable it first, or copy its slug from **My Skills**.
- Commit each skill on the source before you copy it.

> **Before you copy:** check **My Skills** on this deployment for skills with the same names, and archive or rename any you're replacing.

## 2. Run the packer

**Ask Aqua:**

> Run the Skill Packer capsule /capsule/&lt;this capsule's slug&gt; with skills = 1234567, 7654321. Wait for it to finish, then tell me which skills it packed and which it skipped, from the run's output, and give me the computation ID.

**Or do steps 2–4 in one go.** Swap in your own slugs and send this to Aqua in a new chat:

> Run the Skill Packer capsule /capsule/&lt;this capsule's slug&gt; with skills = 1234567, 7654321. When it finishes, read report.json from the run's results. Before creating anything, compare each name in its "packed" list with the custom skills I already have, and skip any packed skill whose name matches one of mine. For each remaining skill, create a separate stand-alone skill whose files are exact byte-for-byte copies of the files listed in that skill's "paths", keeping each file's path relative to its skill folder. Do not edit, reformat or improve anything. Commit each skill. Then list each new skill's name and slug, the skills you skipped because I already have them, the computation ID, and anything the packer skipped with its reason.

Skills you've disabled on this deployment aren't checked, so look in **My Skills** if you've disabled any.

**Or use the App Panel:**

| Field | Parameter | Required | What to enter |
|---|---|---|---|
| Skill slugs or URLs | `skills` | yes | Slugs or pasted URLs, separated by spaces, commas or new lines |
| Your Code Ocean email (optional) | `git_user` | no | Leave empty: the packer looks up the token owner's email, which Git needs as the username. Set it only if the lookup fails (you own no capsules or data assets on the source) |
| Source deployment URL | `source_host` | no | `https://<source-host>`. Defaults to the `SRC_HOST` environment variable |
| Only skills tagged (optional) | `keyword` | no | Pack only skills whose `metadata.tags` include this tag, or whose top-level `tags` do when there's no `metadata.tags` (not case-sensitive) |
| Bundle folder name | `bundle` | no | Defaults to `migrated-skills`. Lowercase letters, digits and hyphens; `output` is reserved |
| Token env var name | `token_env` | no | The env var your secret is attached as. Defaults to `SRC_CO_TOKEN` |

The run takes a few seconds. Its output starts with `Skill Packer 2026.10.08`, then lists what was packed and what was skipped.

## 3. What you get

```
/results/
├─ migrated-skills/
│  ├─ .claude-plugin/plugin.json
│  ├─ skills/<skill-name>/SKILL.md (and every other file in the skill)
│  ├─ README.md        source, date (UTC), packer version, and a table of skills with source slug, commit, files, bytes
│  ├─ CHANGELOG.md
│  └─ SHA256SUMS       SHA-256 checksum of every packed file
├─ report.json         packer version, packed skills and skipped items with reasons
└─ output              the run's log
```

For each packed skill, `report.json` lists its name, source slug, commit, file count and bytes, every file path (`paths`), the files that were executable on the source (`executables`) and, if any, the files that weren't copied (`not_copied`). Dates in the bundle are marked (UTC).

In a repo that holds several skills, each folder with a `SKILL.md` is packed as its own skill. Folders whose names start with `.` (such as `.archive`) are never treated as skills.

The packer copies regular files and folders. Symbolic links and file names with line breaks are never copied. When a skill has any, the output prints `<name>: not copied (symbolic links or unusable file names): …` and `report.json` lists them under `not_copied`. If the skill needs those files, replace them with regular files on the source and run again.

Skipped items always come with a reason. The run never stops because of one bad slug.

| Reason | What to do |
|---|---|
| `no commits; commit the skill on the source` | Open the skill on the source, commit it, run again |
| `regular capsule, not a skill (it has a .codeocean folder)` or `no SKILL.md in the repo; not a skill` | That slug isn't a skill. Copy the slug from **My Skills** instead |
| `not tagged <keyword>` | Expected when you set a keyword |
| `duplicate name; already packed from <slug>` | Two sources have a skill with the same name, ignoring case. Only the first is packed |
| `Couldn't work out your email from the token` | Your token was rejected or you own nothing on the source. Check the secret, or set `git_user` to your email |
| `clone failed: remote: user not found` | `git_user` was set and doesn't match the token's owner. Leave it empty |
| `clone failed: …capsule not found` or another auth error | Wrong slug, or your token can't read that skill |
| `clone failed: … SSL certificate problem` | The source uses a CA this capsule doesn't trust. See “Customize” below |
| `clone failed: … Could not resolve host` or `Failed to connect` | This deployment can't reach the source. A network rule is needed |
| `clone failed: timed out after 600 s` | The clone took more than 10 minutes. Run again with just that slug. If it times out again, check the network path as in “Customize” below |
| `URL is on <host>, not the source <host>` | Slugs differ between deployments. Use URLs from the source deployment |
| `not a 7-digit slug or /capsule/NNNNNNN URL` | Check what you pasted |
| A frontmatter problem (no frontmatter, bad YAML, missing or unsafe `name`) | Fix the skill's `SKILL.md` on the source |

## 4. Create the skills here

Start a new Aqua chat and send this prompt. Fill in the computation ID from step 2; it appears in Aqua's reply or in the run's details.

> Computation &lt;computation id&gt; (a run of the Skill Packer capsule) has results under migrated-skills/ in Claude plugin layout. Read report.json from the results. For each skill in its "packed" list, create a separate stand-alone skill whose files are exact byte-for-byte copies of the files listed in that skill's "paths", keeping each file's path relative to its skill folder. Do not edit, reformat or improve anything. Commit each skill. Report each new skill's name and slug.

Allow 1–2 minutes per 10 KB of skill files. For example, two skills of 11–12 KB each take about 2½ minutes. The new skills belong to you and start Enabled. Share them as needed; sharing on the source doesn't carry over.

## 5. Check the copies

Check that every file in the new skills matches the packed original. You need the packer run's computation ID and the new skills' slugs or URLs from Aqua's reply.

**With the Skill Bridge app (recommended).** In the app's **4. Check the copies** section, enter the computation ID and the new skills' slugs or URLs, then choose **Check copies**. Or start a cloud workstation on this capsule and run this in its terminal:

```bash
python3 /code/skill_bridge.py verify --dst-host https://<this deployment> --computation <computation id> --skills <new skill slugs or URLs>
```

It uses the token in `DST_CO_TOKEN` if that is set. Otherwise it asks for an API token for this deployment and doesn't show what you type. Every file should show `OK`; anything else comes with what to fix, including the commands that make scripts executable again. It exits with 0 when everything matches. If it says it couldn't work out your email, add `--git-user <your email>`.

**Or compare the checksums yourself:**

1. Open the packer run's results and download `migrated-skills/SHA256SUMS`.
2. Start a cloud workstation on this capsule and upload `SHA256SUMS` to its `/scratch` folder.
3. In the workstation terminal, clone each new skill next to it and compare:

```bash
cd /scratch
git clone https://<this-host>/capsule-<new-skill-slug>.git new-skill
cd new-skill
grep "  skills/<skill-name>/" ../SHA256SUMS | sed "s#  skills/<skill-name>/#  #" | sha256sum -c
```

Every line should say `OK`. If Git asks for credentials, the username is your email and the password is an API token for this deployment.

**Executable scripts.** The files in the new skills are regular files, not executable. `report.json` lists the files that were executable on the source under `executables`. If a skill needs one of them to be executable, set it again in a clone of the new skill:

```bash
git update-index --chmod=+x <file>
git commit -m "Make <file> executable"
git push
```

Pushing needs an API token for this deployment with **Capsule write** scope. `git ls-files -s` shows mode `100755` for executable files.

## Use the Skill Bridge app (optional)

The app copies skills in the same way, from one page. It uses the source token you attached for the packer; a token for this deployment is optional.

**Open it:** on this capsule's page, under **or launch a cloud workstation**, choose the **Streamlit** icon. The app opens in the workstation. When you're done, choose **Shut down** (not **Hold**).

- **Settings** come from the capsule, so there's nothing to fill in: the source from `SRC_HOST`, your token from the `SRC_CO_TOKEN` secret, and the Skill Packer capsule ID from `PACKER_CAPSULE_ID`. The target is this deployment. A target token in `DST_CO_TOKEN` is optional.
- **1. Load your skills:** **Load source skills** asks Aqua on the source for your enabled skills. It takes about 30–40 seconds, then shows each skill's name, slug, description, tags, commit and file count. Use **Add skills by slug or URL** for skills that aren't listed, such as disabled ones. **Load target skills** (optional, needs a target token) marks skills whose names already exist here and leaves them unticked.
- **2. Choose skills to copy:** tick the skills you want. Filter by tag, or use **Select all** and **Select none**.
- **3. Copy them with Aqua on the target:** copy the prompt, which has your selected slugs and this capsule's ID, and send it in a new Aqua chat on this deployment. It does steps 2–4 in one go and skips skills whose names you already have. **Run the packer now** (optional, needs a target token and the capsule ID) starts the packer from the app, then shows a shorter prompt that creates the skills.
- **4. Check the copies:** enter the packer run's computation ID, the new skills' slugs or URLs and, if asked, an API token for this deployment, then choose **Check copies**.

## Good to know

- **Text files copy as-is.** If a skill includes binary files, check them after copying.
- **Executable scripts** need their executable bit set again after copying (step 5).
- **Timing:** the packer takes seconds. Listing your skills in the app takes about 30–40 seconds per deployment. Creating the skills takes 1–2 minutes per 10 KB of files.
- **Skills are instructions Aqua follows.** Read the packed `README.md` and the skills themselves before creating them here, especially skills written by someone else.

## Customize for your organization (capsule owner)

| Setting | Where | What it does |
|---|---|---|
| `SRC_HOST` | Environment variable | Default source deployment, e.g. `https://codeocean.dev.example.com`. Users can override it with `source_host` |
| `EXTRA_CA_CERT_URLS` | Environment variable | Space-separated URLs of extra CA certificates (PEM or DER). `environment/postInstall` installs them at build time and logs each certificate's subject, so Git trusts a source whose certificate chains to an internal CA. Leave empty if the source uses a public CA |
| `EXTRA_CA_ALLOW_INSECURE` | Environment variable | Set to `1` only if a CA certificate URL can't be downloaded with verification; the build log says so. Leave it unset otherwise |
| `EXTRA_PIP_PACKAGES` | Environment variable | Space-separated pip packages to install at build time, e.g. `streamlit` for the Skill Bridge app. Leave empty for the packer alone |
| `PACKER_CAPSULE_ID` | Environment variable | This capsule's UUID. The Skill Bridge app puts it in the prompt it builds |
| `source_host` default | App Panel | Alternative to `SRC_HOST`, visible to users |
| `token_env` default | App Panel | Match the env var name of the secret slot users attach (default `SRC_CO_TOKEN`) |
| `bundle` default | App Panel | Default output folder name |

- **Rebuild the environment after changing any environment variable.** When you ask Aqua to change one, list all of them with their values and say “keeping everything else unchanged”.
- **After an environment change,** have Aqua fully shut down the cloud workstation (not hold), then commit.

**Before rolling out, check that this deployment can reach the source** with a reproducible run, not only a workstation, because their network rules can differ. Run the packer on one of your own skills and read the output; the table in step 3 tells you what each failure means.

### Update this capsule from GitHub

Send this to Aqua, with this capsule's UUID:

```text
In capsule <this capsule's UUID>, update the Skill Packer code to the latest version from GitHub. Start a cloud workstation and run exactly this in its terminal:
set -e; rm -rf /tmp/sp; git clone -q --depth 1 https://github.com/codeocean-nate/skill-packer.git /tmp/sp; cp /tmp/sp/code/run /tmp/sp/code/skill_packer.py /code/; chmod +x /code/run; git -C /tmp/sp log -1 --format=%h
Do not change code/README.md, the environment, the secrets or any other file. Make the App Panel parameter git_user optional if it is required, and change nothing else in the App Panel. Then shut down the cloud workstation you started (a full shutdown, not hold), and only after that commit all changes with the message 'Update Skill Packer from GitHub'. Tell me the GitHub commit you updated to.
```

Send it when the capsule has no uncommitted changes, because it commits everything. It updates `code/run` and `code/skill_packer.py`, and keeps `code/README.md`, the environment, the CA certificates and the secrets as they are. The next run's output starts with `Skill Packer 2026.10.08` or a later version.

### Turn on the Skill Bridge app

Send this to Aqua, with this capsule's UUID (twice), your `SRC_HOST`, and your current `EXTRA_CA_CERT_URLS` value or (empty):

```text
In capsule <this capsule's UUID>, start a cloud workstation and run exactly this in its terminal:
set -e; rm -rf /tmp/sp; git clone -q --depth 1 https://github.com/codeocean-nate/skill-packer.git /tmp/sp; cp /tmp/sp/code/run /tmp/sp/code/skill_packer.py /tmp/sp/code/app.py /tmp/sp/code/skill_bridge.py /tmp/sp/code/streamlit_app.py /code/; mkdir -p /code/.streamlit; cp /tmp/sp/code/.streamlit/config.toml /code/.streamlit/; cp /tmp/sp/environment/postInstall /root/capsule/environment/postInstall; chmod +x /code/run /root/capsule/environment/postInstall; git -C /tmp/sp log -1 --format=%h
Then shut down the cloud workstation (a full shutdown, not hold). After that, set up the environment: base image codeocean/ubuntu:22.04; apt packages ca-certificates, curl, git, openssl, python3, python3-pip and python3-yaml; run the post-install script environment/postInstall; environment variables SRC_HOST = <source URL>, EXTRA_CA_CERT_URLS = <current value, or (empty)>, EXTRA_PIP_PACKAGES = streamlit and PACKER_CAPSULE_ID = <this capsule's UUID>, keeping everything else unchanged. Do not change code/README.md, the App Panel or the secrets. Build the environment, show me the post-install part of the build log, and check that the streamlit command exists. Then shut down the cloud workstation you started (a full shutdown, not hold), and only after that commit all changes.
```

It also brings the packer code up to date. Send it when the capsule has no uncommitted changes, because it commits everything. It takes about 3 minutes, including the environment build.

Then open the app as described in “Use the Skill Bridge app”, and choose **Load source skills** once to check that the workstation reaches the source. If the environment build fails at the pip step, set `EXTRA_PIP_PACKAGES` back to empty, listing all four variables with their values, and rebuild. The packer keeps working without the app.

Environment: `codeocean/ubuntu:22.04` with apt packages `ca-certificates`, `curl`, `git`, `openssl`, `python3`, `python3-pip` and `python3-yaml`. The App Panel uses named parameters, which reach the script as `--name=value`, with empty optional fields left out. The email lookup reads `owner_email` from a capsule or data asset search limited to `ownership: private`. `skill_packer.py` needs Python 3.9+ and PyYAML. The Skill Bridge app needs Streamlit, installed through `EXTRA_PIP_PACKAGES`.
