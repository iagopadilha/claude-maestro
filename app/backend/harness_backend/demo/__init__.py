"""Demo mode (`harness.py demo`): a fake project with mocked, moving loop data, served by the real app.

Nothing here calls `claude` or touches the user's data folder: the project, its git repo, the Claude Code style transcripts
and the app data all live in a temporary (or chosen) folder, and the `claude` binary and the engine scripts the panel could
trigger are replaced by stubs that only print a demo notice.

  demo_story_catalog.py      the story: features, stages, texts and the turn plan (data only)
  demo_workspace.py          folders of one demo run, atomic file writes and the demo git repo
  demo_transcript_writer.py  writes the Claude Code transcripts (orchestrator + subagents) of a finished turn
  demo_story_player.py       applies the story one step at a time to the files the panel reads
  demo_project_seeder.py     creates the project, its `.specs/` and the turns that already happened; resets the story
  demo_animator.py           thread that advances the story every few seconds (scaled by --speed), optionally forever
  demo_runner.py             `harness.py demo`: wires the seeder, the animator and the app, and cleans up
  stubs/                     stand-ins for `claude` and the engine scripts (print a notice, never run a loop)
"""
