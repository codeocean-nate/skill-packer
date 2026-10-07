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

1. **Create the capsule from this repository.** In Aqua: “Create a new capsule by copying this Git repository: https://github.com/codeocean-nate/skill-packer. Keep it independent of the repository.” Or use the **Copy from Git** option in the Create menu.
2. **Point it at your source deployment.** Set the `SRC_HOST` environment variable to `https://<source-host>`, or set a default for the `source_host` App Panel parameter.
3. **If the source uses an internal CA,** set `EXTRA_CA_CERT_URLS` to the CA certificates' URLs, separated by spaces, and rebuild the environment. `environment/postInstall` installs them.
4. **Check the network path:** run it once on one of your own skills, as a reproducible run. See `code/README.md` for what each failure means.
5. **Share or release it.** Each user attaches their own source token as a secret. The capsule ships with an empty `SRC_CO_TOKEN` slot.

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
