name: crous-velodrome-watch

# GitHub kills long runs, so coverage is a chain of runs rather than one
# process. The cron fires every 5 minutes, but the concurrency lock lets only
# one run at a time: the others wait in the queue and start the instant the
# current one ends.

on:
  schedule:
    - cron: "*/5 * * * *"
  workflow_dispatch:

permissions:
  contents: write

concurrency:
  group: crous-watch
  cancel-in-progress: false

jobs:
  watch:
    runs-on: ubuntu-latest
    timeout-minutes: 70
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - run: pip install requests beautifulsoup4

      - name: Watch
        env:
          NTFY_TOPIC: ${{ secrets.NTFY_TOPIC }}
          KEYWORDS: "VELODROME,CHARMOIS"
          POLL_SECONDS: "5"
          RUN_SECONDS: "3540"
        run: python crous_watch.py

      - name: Persist state
        if: always()
        run: |
          git config user.name "crous-watch"
          git config user.email "actions@github.com"
          git add known.json || true
          git diff --staged --quiet || git commit -m "state $(date -u +%FT%TZ)"
          git push || true
