"""Routes of the projects hub (/api/app/projects*, /api/app/system/folders) and of the project checklist
(/p/<id>/api/progress, structure/initialize, conformance). Only translates HTTP <-> service."""
import os

from ..router import Response
from .project_responses import project_to_dict, respond_converting_errors


class ProjectController:
    def __init__(self, project_service, project_progress_service, specs_structure_service, folder_picker_service, log):
        self.project_service = project_service
        self.project_progress_service = project_progress_service
        self.specs_structure_service = specs_structure_service
        self.folder_picker_service = folder_picker_service
        self.log = log

    def register_routes(self, router):
        router.add("GET", "/api/app/projects", self.list_projects)
        router.add("POST", "/api/app/projects/open", self.open_project)
        router.add("POST", "/api/app/projects/create", self.create_project)
        router.add("POST", "/api/app/projects/pick-folder", self.pick_folder)
        router.add("POST", "/api/app/projects/{project_id}/forget", self.forget_project)
        router.add("GET", "/api/app/system/folders", self.list_folders)
        router.add("GET", "/p/{project_id}/api/progress", self.get_progress)
        router.add("POST", "/p/{project_id}/api/structure/initialize", self.initialize_structure)
        router.add("POST", "/p/{project_id}/api/conformance", self.write_conformance)

    # ------------------------------------------------ hub
    def list_projects(self, request):
        return Response.json(200, {"projects": [self.build_list_item(project) for project in self.project_service.list_projects()]})

    def build_list_item(self, project):
        item = project_to_dict(project)
        item["folder_exists"] = os.path.isdir(project.repo_path)
        try:
            item["progress_summary"] = self.project_progress_service.build_progress_summary(self.project_service.get_project_context(project.id))
        except Exception as failure:  # one unreadable project does not take the list down
            self.log.warn("progress_summary_failed", project=project.id, type=type(failure).__name__, message=str(failure)[:200])
            item["progress_summary"] = None
        return item

    def open_project(self, request):
        return respond_converting_errors(lambda: self.build_project_response(
            self.project_service.open_existing_project(request.body.get("path"))))

    def create_project(self, request):
        body = request.body
        return respond_converting_errors(lambda: self.build_project_response(self.project_service.create_project_from_scratch(
            body.get("parent_path"), body.get("name"), init_git=body.get("init_git", True) is not False,
            conformance=body.get("conformance") or None)))

    def build_project_response(self, project):
        data = project_to_dict(project)
        return Response.json(200, {"ok": True, "project": data, "url": data["url"]})

    def pick_folder(self, request):
        return Response.json(200, self.folder_picker_service.pick_folder_with_dialog())

    def forget_project(self, request):
        def forget():
            self.project_service.forget_project(request.route_params["project_id"])
            return Response.json(200, {"ok": True})
        return respond_converting_errors(forget)

    def list_folders(self, request):
        return respond_converting_errors(lambda: Response.json(200, self.folder_picker_service.list_subfolders(
            request.get_query("path"))))

    # ------------------------------------------------ project
    def get_progress(self, request):
        return respond_converting_errors(lambda: Response.json(200, self.project_progress_service.build_progress(
            self.project_service.get_project_context(request.route_params["project_id"]))))

    def initialize_structure(self, request):
        project_id = request.route_params["project_id"]

        def initialize():
            result = self.specs_structure_service.initialize_project_structure(
                self.project_service.get_project_context(project_id), request.body.get("conformance") or None)
            context = self.project_service.get_project_context(project_id)  # registers the MONITOR.json now that .specs/ exists
            return Response.json(200, dict(result, ok=True, progress=self.project_progress_service.build_progress(context)))
        return respond_converting_errors(initialize)

    def write_conformance(self, request):
        def write():
            value = self.specs_structure_service.write_project_conformance(
                self.project_service.get_project_context(request.route_params["project_id"]), request.body.get("value"))
            return Response.json(200, {"ok": True, "conformance": value})
        return respond_converting_errors(write)
