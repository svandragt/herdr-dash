# Releasing

A release tags a known-good commit on `main` and publishes notes for people
who install by cloning. There is no package and no release workflow.

## Choose the version

Use `vMAJOR.MINOR.PATCH`. While the version starts with `0`, raise MINOR for
a new feature or a change to the registry file or the dot-title rule, and
PATCH for a fix.

## Check the commit

1. Run the self-check:

   ```sh
   python3 test_fresh.py
   ```

2. Restart the board and confirm that cards appear for your local and remote
   herdr servers.

## Tag and publish

1. Write the notes to a file outside the repo, for example
   `/tmp/notes-v0.2.0.md`. Use these sections, in this order: Highlights,
   Upgrade, Details, How we checked, Known gaps, Issues closed. Leave out a
   section that has no content. Write for someone who uses the board but did
   not follow the work.
2. Tag and push:

   ```sh
   git tag -a v0.2.0 -m v0.2.0
   git push origin v0.2.0
   ```

3. Create the release from the tag:

   ```sh
   gh release create v0.2.0 --title v0.2.0 --notes-file /tmp/notes-v0.2.0.md
   ```

Never publish the auto-generated commit list as the notes.
