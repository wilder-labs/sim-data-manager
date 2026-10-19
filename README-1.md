# Wilder Labs Studio

The studio's private hub. Every project gets one folder under `projects/`, and every project folder says where its working files live on Jef's computer.

## How this repo works

- **One folder per project.** `projects/<project-name>/`, lowercase and hyphenated.
- **Every project has a `project.md`.** It names the linked folder on Jef's computer, the live addresses, what is in the folder and what isn't, and the current status. A project without a `project.md` doesn't go in.
- **Folders, not branches, for kinds of work.** Site code, content, outreach and media are folders inside the project. Branches are only for a piece of work in progress (a redesign, a big refactor), merged back when done.
- **Small files only.** Code, markdown, CSV, JSON and finished images under a few MB. Raw video, project files (PSD, AI, PRPROJ), zips and anything over about 25 MB stay in Drive or Creative Cloud. The project's `project.md` lists where they are.
- **No secrets, ever.** No passwords, API keys, upload links, `.env` files or personal data files. `.gitignore` blocks the common ones, but check before every commit.

## Projects

| Project | Repo folder | Linked folder on Jef's computer | Live | Status |
|---|---|---|---|---|
| wilderlabs.studio site | `projects/wilderlabs-site/` | `C:\WILDERS DESIGN PRO\wilder-labs-studio-brand\03-web-divi` | https://wilderlabs.studio | Live sections: Sim Tools, Personal Tools, Blog, Website check. Coming soon page on the home page until launch. |
| WordPress plugin guide | `projects/wordpress-plugin-guide/` | `C:\WILDERS DESIGN PRO\wordpress-plugin-guide` | Not public yet | Draft v0.1 reference guide |

Add a row here whenever a project is added.

## Related repos

- `wilder-labs/sim-data-manager`: Sim Data Manager source and public releases. Kept separate because it is public and has its own license.

## Working with Claude

See `docs/working-with-claude.md`.
