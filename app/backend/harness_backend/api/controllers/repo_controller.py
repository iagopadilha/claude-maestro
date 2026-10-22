"""Routes of a project's repo list (hub-level, keyed by project id): list, add, remove, change the role (edit | reference), the copy
list and its suggestions and the scope confirmation. The repos to touch come from the planning chat (no separate AI proposal). Only translates HTTP <-> RepoRegistryService; nothing here touches a folder on disk."""
from ..router import Response
from .project_responses import respond_converting_errors

REPOS_ROUTE = "/api/app/projects/{project_id}/repos"


class RepoController:
    def __init__(self, project_service, repo_registry_service):
        self.project_service = project_service
        self.repo_registry_service = repo_registry_service

    def register_routes(self, router):
        router.add("GET", REPOS_ROUTE, self.list_repos)
        router.add("POST", REPOS_ROUTE, self.add_repo)
        router.add("POST", REPOS_ROUTE + "/scope/confirm", self.confirm_scope)
        router.add("PATCH", REPOS_ROUTE + "/{name}", self.update_repo)
        router.add("PUT", REPOS_ROUTE + "/{name}/role", self.set_role)  # same handler, works with the current HTTP server
        router.add("DELETE", REPOS_ROUTE + "/{name}", self.remove_repo)
        router.add("PUT", REPOS_ROUTE + "/{name}/copy", self.set_copy_list)
        router.add("PUT", REPOS_ROUTE + "/{name}/setup", self.set_setup)
        router.add("GET", REPOS_ROUTE + "/{name}/copy-suggestions", self.list_copy_suggestions)

    def respond_with_context(self, request, action_with_context):
        return respond_converting_errors(lambda: Response.json(200, action_with_context(
            self.project_service.get_project_context(request.route_params["project_id"]))))

    def list_repos(self, request):
        return self.respond_with_context(request, self.repo_registry_service.list_repos)

    def add_repo(self, request):
        return self.respond_with_context(request, lambda context: self.repo_registry_service.add_repo(
            context, request.body.get("path"), request.body.get("copy"), request.body.get("setup"), request.body.get("role"), request.body.get("reason")))

    def set_role(self, request):
        return self.respond_with_context(request, lambda context: self.repo_registry_service.set_role(
            context, request.route_params["name"], request.body.get("role")))

    def update_repo(self, request):
        return self.respond_with_context(request, lambda context: self.repo_registry_service.update_repo(
            context, request.route_params["name"], request.body.get("role"), request.body.get("reason")))

    def confirm_scope(self, request):
        return self.respond_with_context(request, self.repo_registry_service.confirm_scope)

    def remove_repo(self, request):
        return self.respond_with_context(request, lambda context: self.repo_registry_service.remove_repo(context, request.route_params["name"]))

    def set_copy_list(self, request):
        return self.respond_with_context(request, lambda context: self.repo_registry_service.set_copy_list(
            context, request.route_params["name"], request.body.get("copy", "")))  # explicit null = automatic; a missing key stays invalid

    def set_setup(self, request):
        return self.respond_with_context(request, lambda context: self.repo_registry_service.set_setup(
            context, request.route_params["name"], request.body.get("setup")))

    def list_copy_suggestions(self, request):
        return self.respond_with_context(request, lambda context: self.repo_registry_service.list_copy_suggestions(
            context, request.route_params["name"]))
