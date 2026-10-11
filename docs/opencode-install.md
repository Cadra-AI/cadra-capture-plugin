## OpenCode

OpenCode does not read Claude plugins, so one script copies the three Cadra skills and
the `/cadra-*` commands into your OpenCode config. It needs only Python 3.

1. Clone this repo (skip if you already have it):

   ```bash
   git clone <REPO_URL> cadra-plugin
   ```

2. Run the installer.

   macOS / Linux:

   ```bash
   python3 cadra-plugin/plugins/cadra-trace-tracker/scripts/install_opencode.py
   ```

   Windows (PowerShell):

   ```powershell
   py -3 cadra-plugin\plugins\cadra-trace-tracker\scripts\install_opencode.py
   ```

   This installs globally into `~/.config/opencode` (`%USERPROFILE%\.config\opencode`
   on Windows). To install for one project only, add `--project <DIR>`. To preview
   without writing anything, add `--dry-run`.

3. Restart OpenCode.

4. In your solution folder, type `/cadra-connect`, then `/cadra-submit` when you are done.
   `/cadra-traces` lists what the server holds.

No environment variables are needed. The installer writes the plugin's location into
the copied skills and does not touch your shell profile.

Check the install (write to a file first; piping `opencode debug skill` straight into
`grep` can truncate the output):

```bash
opencode debug skill > /tmp/skills.json; grep cadra /tmp/skills.json
```

```powershell
opencode debug skill > $env:TEMP\skills.json; Select-String cadra $env:TEMP\skills.json
```

Update: `git pull`, then run the installer again.

Uninstall: run the installer with `--uninstall`. It removes only the three Cadra skills
and three command files it created.

Moving or renaming the cloned folder breaks the copies; run the installer again.
