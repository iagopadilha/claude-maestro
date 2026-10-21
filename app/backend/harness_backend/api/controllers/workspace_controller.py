"""Panel-level routes of the workspace, of Close loop and of the wrap-up (Finish) (`/p/<project_id>/api/...`) and the hub overview of the running loops
(`GET /api/app/overview`). Only translates HTTP <-> service."""
import datetime

from ..router import Response
from .loop_commands_controller import PROJECT_ROUTE_PREFIX
from .project_responses import respond_converting_errors


class WorkspaceController:
    def __init__(self, project_service, workspace_service, loop_worktree_service, loop_status_service, wrap_up_service=None):
        self.project_service = project_service
        self.workspace_service = workspace_service
        self.loop_worktree_service = loop_worktree_service
        self.loop_status_service = loop_status_service
        self.wrap_up_service = wrap_up_service  # Finish: only the wrap-up routes need it

    def register_routes(self, router):
        router.add("GET", PROJECT_ROUTE_PREFIX + "/workspace", self.get_workspace)
        router.add("POST", PROJECT_ROUTE_PREFIX + "/workspace/create", self.create_workspace)
        router.add("GET", PROJECT_ROUTE_PREFIX + "/workspace/job/{job_id}", self.get_create_job)
        router.add("POST", PROJECT_ROUTE_PREFIX + "/workspace/repos/{name}/commit-push", self.commit_and_push)
        router.add("POST", PROJECT_ROUTE_PREFIX + "/workspace/repos", self.add_repo)  # the contract's repo list routes (same service as the hub's)
        router.add("PATCH", PROJECT_ROUTE_PREFIX + "/workspace/repos/{name}", self.update_repo)
        router.add("DELETE", PROJECT_ROUTE_PREFIX + "/workspace/repos/{name}", self.remove_repo)
        router.add("GET", PROJECT_ROUTE_PREFIX + "/close-loop/check", self.check_close_loop)
        router.add("POST", PROJECT_ROUTE_PREFIX + "/close-loop", self.close_loop)
        if self.wrap_up_service is not None:
            router.add("GET", PROJECT_ROUTE_PREFIX + "/wrap-up", self.get_wrap_up)
            router.add("POST", PROJECT_ROUTE_PREFIX + "/wrap-up/finish", self.finish_wrap_up)
        router.add("GET", "/api/app/overview", self.get_overview)

    def respond_with_context(self, request, action_with_context):
        return respond_converting_errors(lambda: Response.json(200, action_with_context(
            self.project_service.get_project_context(request.route_params["project_id"]))))

    def get_workspace(self, request):
        return self.respond_with_context(request, self.workspace_service.get_workspace)

    def create_workspace(self, request):
        return self.respond_with_context(request, lambda context: self.workspace_service.create_workspace(context, request.body.get("repos")))

    def commit_and_push(self, request):
        name, body = request.route_params["name"], request.body
        return self.respond_with_context(request, lambda context: self.workspace_service.commit_and_push(
            context, name, body.get("message"), body.get("push", True)))

    def add_repo(self, request):
        body = request.body
        return self.respond_with_context(request, lambda context: self.workspace_service.repo_registry_service.add_repo(
            context, body.get("path"), body.get("copy"), body.get("setup"), body.get("role"), body.get("reason")))

    def update_repo(self, request):
        name, body = request.route_params["name"], request.body
        return self.respond_with_context(request, lambda context: self.workspace_service.repo_registry_service.update_repo(
            context, name, body.get("role"), body.get("reason")))

    def remove_repo(self, request):
        name = request.route_params["name"]
        return self.respond_with_context(request, lambda context: self.workspace_service.repo_registry_service.remove_repo(context, name))

    def get_create_job(self, request):
        job_id = request.route_params["job_id"]
        return self.respond_with_context(request, lambda context: self.workspace_service.get_create_job(context, job_id))

    def check_close_loop(self, request):
        return self.respond_with_context(request, self.loop_worktree_service.check_close_loop)

    def close_loop(self, request):
        body = {name: value for name, value in request.body.items() if name not in ("force_remove", "finished")}  # forcing and finishing go through wrap-up/finish only (typed confirmation)
        return self.respond_with_context(request, lambda context: self.loop_worktree_service.close_loop(context, body))

    def get_wrap_up(self, request):
        return self.respond_with_context(request, self.wrap_up_service.get_wrap_up)

    def finish_wrap_up(self, request):
        return self.respond_with_context(request, lambda context: self.wrap_up_service.finish(context, request.body))

    def get_overview(self, request):
        return Response.json(200, {"ok": True, "at": datetime.datetime.now().isoformat(timespec="seconds"),
                                   "loops": self.loop_status_service.list_running_loops()})
