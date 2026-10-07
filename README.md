# Skill Packer

A Code Ocean capsule that copies Aqua custom skills from one Code Ocean deployment to another, using only the two deployments' own Git. Each user copies their own skills with their own read-only token.

```
source deployment            this capsule (destination)              destination Aqua
My Skills ── git clone ──▶  /results/migrated-skills/  ── reads ──▶  one new skill per folder
(your skills)  (your token)   .claude-plugin/plugin.json               (owned by you)
                              skills/<name>/SKILL.md …
                              README.md, CHANGELOG.md, SHA256SUMS
```

1. On the source deployment, copy the slugs of the skills you want from **My Skills**.
2. Run this capsule on the destination with those slugs. It clones each skill over Git and writes them in Claude plugin layout, with a checksum for every file.
3. Ask Aqua on the destination to create one skill per packed folder, then check the copies against `SHA256SUMS`.

Code Ocean's public API can't list or create skills, so a person copies the slugs (step 1) and Aqua creates the skills (step 3).

## Set it up on a Code Ocean deployment

1. **Create the capsule from this repository.** In Aqua: “Create a new capsule named skill-packer by copying this Git repository: https://github.com/codeocean-nate/skill-packer. Use the repository as the starting point, but create an independent capsule that is not linked to the Git repository. Tell me its slug and UUID.” Or use **Copy from Git** in the Create menu. Everything comes across: code, environment, App Panel and an empty `SRC_CO_TOKEN` secret slot.
2. **Attach your source token right away.** Create an API token on the source deployment (Capsule read scope), save it as a secret on this deployment, and attach it to the `SRC_CO_TOKEN` slot (Environment → Secrets), then commit. Code Ocean won't build or run the capsule while a declared secret slot is empty, and Aqua can't attach secrets.
3. **Point it at your source deployment.** Set the `SRC_HOST` environment variable to `https://<source-host>`, or set a default for the `source_host` App Panel parameter.
4. **If the source uses an internal CA,** set `EXTRA_CA_CERT_URLS` to the CA certificates' URLs, separated by spaces, and rebuild the environment. `environment/postInstall` installs them and logs each certificate's subject.
5. **Check the network path:** run it once on one of your own skills, as a reproducible run. See `code/README.md` for what each failure means.
6. **Share or release it.** Each user attaches their own source token to the slot on their first run.

Full usage, parameters, skip reasons, the Aqua prompt and the checksum check are in [`code/README.md`](code/README.md).

## Files

| Path | Purpose |
|---|---|
| `code/run` | Entry point |
| `code/skill_packer.py` | Clones, filters by tag, packs, writes checksums and `report.json` (Python 3.9+, PyYAML) |
| `code/README.md` | User guide shown in the capsule |
| `environment/postInstall` | Installs extra CA certificates from `EXTRA_CA_CERT_URLS`; does nothing when it's empty |
| `.codeocean/environment.json` | `codeocean/ubuntu:22.04` with ca-certificates, curl, git, openssl, python3, python3-yaml; env vars `SRC_HOST`, `EXTRA_CA_CERT_URLS` |
| `.codeocean/app-panel.json` | Named parameters: `skills`, `git_user`, `source_host`, `keyword`, `bundle`, `token_env` |
| `.codeocean/secrets.json` | An empty secret slot, `SRC_CO_TOKEN`. No values |

## Limits

- Aqua recreates skills by retyping text: text files only, no executable bits, roughly 12 KB a minute. Always check against `SHA256SUMS`.
- Skills must be committed on the source.
- Copies belong to the user who runs the migration. Sharing settings don't carry over.
- Skills are instructions Aqua follows. Only copy skills you trust.
