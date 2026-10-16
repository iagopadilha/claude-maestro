"""`harness.py demo`: seeds the demo project, starts the app on an isolated data folder, opens the project's panel and runs
the animator until Ctrl+C (or the app's shutdown). Then removes the demo files unless --keep.

The app is the real one (same composition as `serve`) with three demo overrides: the transcripts folder points to the demo's
fake Claude Code transcripts, and `claude` plus the engine scripts the panel can trigger are stubs that only print a notice.
"""
import argparse
import os

from .demo_animator import DemoAnimator, compute_story_seconds
from .demo_project_seeder import CLAUDE_STUB_PATH, ENGINE_STUB_PATH, DemoProjectSeeder
from .demo_story_catalog import build_turn_plans
from .demo_workspace import DemoWorkspace

DATA_DIR_VARIABLE = "LOOP_DATA_DIR"
EXIT_OK = 0
EXIT_FAILURE = 1
DEMO_IDLE_HOURS = 0  # the demo app never shuts itself down for idleness (a recorder may leave it alone for a long time)


def parse_demo_options(args):
    parser = argparse.ArgumentParser(prog="harness.py demo", description="Opens the panel on a fake project with mocked, moving loop data "
                                                                        "(never calls claude, never touches your data folder)")
    parser.add_argument("--port", type=int, default=0, help="fixed port (default: first free one from 47800 to 47899)")
    parser.add_argument("--no-browser", action="store_true", help="does not open the browser")
    parser.add_argument("--speed", type=float, default=1.0, help="animation speed multiplier (2 = twice as fast)")
    parser.add_argument("--data-dir", default="", help="app data folder; the demo project goes in <data-dir>/demo/ (default: a temporary folder)")
    parser.add_argument("--loop", action="store_true", help="restart the story after it ends, forever (for screen recorders)")
    parser.add_argument("--keep", action="store_true", help="keep the demo files when it stops")
    options = parser.parse_args(args)
    if options.speed <= 0:
        parser.error("--speed must be greater than 0")
    return options


def build_demo_application(workspace, options):
    from .. import composition
    os.environ[DATA_DIR_VARIABLE] = workspace.app_data_dir  # engine children and this process read the isolated folder
    return composition.build_application(
        user_data_dir=workspace.app_data_dir, fixed_port=options.port, open_browser=not options.no_browser, idle_hours=DEMO_IDLE_HOURS,
        claude_transcripts_dir=workspace.transcripts_dir, claude_binary=CLAUDE_STUB_PATH,
        engine_script_overrides={"run_loop": ENGINE_STUB_PATH, "delivery": ENGINE_STUB_PATH, "loop_summary": ENGINE_STUB_PATH})


def run_demo(args):
    from .. import composition
    options = parse_demo_options(args)
    workspace = DemoWorkspace.create(options.data_dir)
    seeder = DemoProjectSeeder(workspace)
    application = None
    animator = None
    try:
        player = seeder.seed()
        application = build_demo_application(workspace, options)
        animator = DemoAnimator(seeder, player, application.log, speed=options.speed, loop_forever=options.loop)
        print("DEMO_DIR=" + workspace.base_dir, flush=True)
        print("DEMO_CYCLE_SECONDS=%d" % round(compute_story_seconds(build_turn_plans(), options.speed)), flush=True)
        animator.start()
        composition.start_application(application, initial_project_path=workspace.repo_dir)
    except KeyboardInterrupt:
        pass
    except (OSError, RuntimeError) as failure:
        print("could not run the demo: %s" % failure)
        return EXIT_FAILURE
    finally:
        stop_demo(workspace, seeder, application, animator, options.keep)
    return EXIT_OK


def stop_demo(workspace, seeder, application, animator, keep):
    if animator is not None:
        animator.stop()
        animator.join(timeout=10)
    if keep:
        print("demo files kept in " + workspace.base_dir, flush=True)
        return
    if application is not None and not workspace.is_temporary:  # a chosen --data-dir keeps its registry clean
        try:
            for project in application.projects_module.project_service.list_projects():
                if os.path.normcase(project.repo_path) == os.path.normcase(seeder.repo_dir):
                    application.projects_module.project_service.forget_project(project.id)
        except (OSError, ValueError):
            pass
    if application is not None:
        application.log.info("demo_files_removed", path=workspace.base_dir)
    workspace.remove()
