# The loop runtime moved to `app/`

The unsupervised loop engine (`run_loop.py`, `loop_state.py`, `notify.py`, `delivery.py`, `loop_summary.py`, `rules.py`,
`audit_deps.py`, `graph_view.py`, `scale.py`) and its tests now live in the Loop Harness app, next to this plugin in the
same repository: `app/backend/harness_backend/engine/`.

Run everything through the app CLI (`<repo>` = the project folder):

```
python app/harness.py check <repo>                 # preflight: prints READY and the command, creates nothing
python app/harness.py loop <repo> --wave 1         # the loop (all run_loop.py flags are accepted)
python app/harness.py serve --project <repo>       # the web panel
python app/harness.py state|notify|summary|deliver|rules|audit|graph|classify|scale ...   # the engine scripts
python app/harness.py install                      # lets the skill find the app (~/.loop-harness/config.json)
```

The skill finds the app through `LOOP_APP_HOME`, or `app_root_dir` in `<LOOP_DATA_DIR or ~/.loop-harness>/config.json`.
See `app/docs/HOW_TO_USE.md`.
