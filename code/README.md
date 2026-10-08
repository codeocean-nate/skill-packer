# Skill Packer

Copies your Aqua skills from another Code Ocean deployment (the **source**) into this one, without an external Git host. The capsule clones each skill over the source deployment's own Git and packs the skills into `/results` in Claude plugin layout. Aqua on this deployment then turns each packed folder into a separate skill that you own.

## Before your first run (once)

1. **Create a token on the source deployment.** Account → Access tokens, with **Capsule read** scope.
2. **Save it as a secret on this deployment.** Account → Secrets, Custom Key type.
3. **Attach it to this capsule.** Code Ocean won't run the capsule until the `SRC_CO_TOKEN` slot has a secret. On your first run it shows “Missing Credentials”. Choose **Fix Credentials** and pick your secret. Every user picks their own; nobody else's secret is ever used for your run. Viewers can do this too.

   The capsule reads the token from the env var named in `token_env` (default `SRC_CO_TOKEN`).

> **Capsule owners:** attaching or changing a secret slot leaves `.codeocean/secrets.json` uncommitted. Commit it right away, or Git pushes to this capsule fail with `403 … uncommitted changes in the Code Ocean IDE`. The file holds only the slot name, never the token.

## 1. Find the skills to copy

On the source deployment, open **My Skills** and copy each skill's URL (`…/capsule/1234567/tree`) or just its 7-digit slug. Filter by tag first if you only want some.

- Or ask Aqua on the source deployment. Only this wording works, because asking for a plain list makes Aqua search capsules, and that search never returns skills: “Activate every skill in your available-skills list with your skills tool, one call per skill. For each, report the UUID of the capsule its SKILL.md was loaded from (or 'none' if it was not loaded from a capsule). Then call get_capsule on each such UUID and report the capsule's name and slug as a JSON array. Do not create, change or delete anything.” It only finds skills that are enabled.
- A skill must be **committed** on the source. A skill that was never committed has an empty Git history and is skipped.

## 2. Run the packer

**From the App Panel:**

| Field | Parameter | Required | What to enter |
|---|---|---|---|
| Skill slugs or URLs | `skills` | yes | Slugs or pasted URLs, separated by spaces, commas or new lines |
| Your Code Ocean email (optional) | `git_user` | no | Leave empty: the packer looks up the token owner's email, which Git needs as the username. Set it only if the lookup fails (you own no capsules or data assets on the source) |
| Source deployment URL | `source_host` | no | `https://<source-host>`. Defaults to the `SRC_HOST` environment variable |
| Only skills tagged (optional) | `keyword` | no | Pack only skills whose `metadata.tags` include this tag (not case-sensitive) |
| Bundle folder name | `bundle` | no | Defaults to `migrated-skills` |
| Token env var name | `token_env` | no | The env var your secret is attached as. Defaults to `SRC_CO_TOKEN` |

**Or ask Aqua:**

> Run the Skill Packer capsule /capsule/&lt;this capsule's slug&gt; with skills = 1234567, 7654321. Tell me which skills it packed and which it skipped.

The run usually takes a few seconds.

> **Note:** a run can show **Succeeded** even when it exited with an error. Read the output to see what was packed.

## 3. What you get

```
/results/
├─ migrated-skills/
│  ├─ .claude-plugin/plugin.json
│  ├─ skills/<skill-name>/SKILL.md (and every other file in the skill)
│  ├─ README.md        table of skills with source slug, commit, file count, bytes
│  ├─ CHANGELOG.md
│  └─ SHA256SUMS       checksum of every packed file
├─ report.json         packed skills (with every file path) and skipped items with reasons
└─ output
```

Skipped items always come with a reason. The run never stops because of one bad slug.

| Reason | What to do |
|---|---|
| `no commits; commit the skill on the source` | Open the skill on the source, commit it, run again |
| `no SKILL.md in the repo; not a skill (regular capsule?)` | You pasted a regular capsule's slug. Copy the slug from **My Skills** instead |
| `not tagged <keyword>` | Expected when you set a keyword |
| `duplicate name; already packed from <slug>` | Two sources have a skill with the same name. Only the first is packed |
| `clone failed: remote: user not found` | `git_user` was set and doesn't match the token's owner. Leave it empty |
| `clone failed: …capsule not found` or another auth error | Wrong slug, or your token can't read that skill |
| `clone failed: … SSL certificate problem` | The source uses a CA this capsule doesn't trust. See “Customize” below |
| `clone failed: … Failed to connect` or `timed out` | This deployment can't reach the source. A network rule is needed |
| `URL is on <host>, not the source <host>` | Slugs differ between deployments. Use URLs from the source deployment |
| `not a 7-digit slug or /capsule/NNNNNNN URL` | Check what you pasted |
| A frontmatter problem (no frontmatter, bad YAML, missing or unsafe `name`) | Fix the skill's `SKILL.md` on the source |

## 4. Create the skills here

Start a new Aqua chat and send this prompt. Fill in the computation ID from step 2; it appears in Aqua's reply or in the run's details.

> Computation &lt;computation id&gt; (a run of the Skill Packer capsule) has results under migrated-skills/ in Claude plugin layout. Read report.json from the results. For each skill in its "packed" list, create a separate stand-alone skill whose files are exact byte-for-byte copies of the files listed in that skill's "paths", keeping each file's path relative to its skill folder. Do not edit, reformat or improve anything. Commit each skill. Report each new skill's name and slug.

Aqua reads each file and writes it into a new skill. Allow about 1 minute per 10–15 KB of files. The new skills belong to you and start Enabled. They aren't shared with anyone, even if the originals were.

> **Name clashes:** if you already have a skill with the same name here, Aqua will have two. Archive or rename one.

## 5. Check the copies

Aqua copies by retyping text, and once in testing it changed two `—` characters into `â`. Download `migrated-skills/SHA256SUMS` from the packer run's results into a cloud workstation, then compare each new skill against it:

```bash
git clone https://<this-host>/capsule-<new-skill-slug>.git new-skill
cd new-skill
grep "  skills/<skill-name>/" ../SHA256SUMS | sed "s#  skills/<skill-name>/#  #" | sha256sum -c
```

Every line should say `OK`. For Git authentication, the username is your email and the password is an API token for this deployment.

## Limits

- **Text files only.** Aqua can't recreate binary files, and executable bits are lost. For skills like that, push the packed folder into a skill over Git instead.
- **Speed:** roughly 12 KB of skill files a minute in the Aqua step. The packer itself takes seconds.
- **Skills are instructions Aqua follows.** Read the packed `README.md` and the skills themselves before creating them here, especially skills written by someone else.

## Customize for your organization (capsule owner)

| Setting | Where | What it does |
|---|---|---|
| `SRC_HOST` | Environment variable | Default source deployment, e.g. `https://codeocean.dev.example.com`. Users can override it with `source_host` |
| `source_host` default | App Panel | Alternative to `SRC_HOST`, visible to users |
| `token_env` default | App Panel | Match the env var name of the secret slot users attach (default `SRC_CO_TOKEN`) |
| `bundle` default | App Panel | Default output folder name |
| `EXTRA_CA_CERT_URLS` | Environment variable, then **rebuild** | Space-separated URLs of extra CA certificates (PEM or DER). `environment/postInstall` installs them at build time, so Git trusts a source whose certificate chains to an internal CA. Leave empty if the source uses a public CA |

**Before rolling out, check that this deployment can reach the source** with a reproducible run, not only a workstation, because their network rules can differ. Run the packer on one of your own skills and read the output; the table in step 3 tells you what each failure means.

Environment: `codeocean/ubuntu:22.04` with apt packages `ca-certificates`, `curl`, `git`, `openssl`, `python3` and `python3-yaml`. The App Panel uses named parameters, which reach the script as `--name=value`, with empty optional fields left out. The email lookup uses the public API: a capsule or data asset search limited to `ownership: private` returns only the token owner's items, each with `owner_email`; the API has no “whoami” endpoint. `skill_packer.py` needs Python 3.9+ and PyYAML.
