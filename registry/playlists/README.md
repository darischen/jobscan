# Playlists

A playlist is a registry CSV with the exact same columns as
`registry/companies.csv`. Run one with `--file`:

    python jobscan.py --file registry/playlists/ai-labs.csv

Keep the master registry complete and use playlists to slice it. The
columns never change, so a playlist row pastes straight back into the
master file.
