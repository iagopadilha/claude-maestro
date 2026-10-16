"""Roadmap and claude job routes: /p/<id>/api/roadmap, roadmap/approve, roadmap/generate, roadmap/refine, codebase/map,
jobs, jobs/<job>. Approving requires the token (POST) and `confirm:true`: only the user's click approves."""
from ..router import Response
from .project_responses import respond_converting_errors

JOB_ACCEPTED_STATUS = 202


class RoadmapController:
    def __init__(self, project_service, roadmap_service, claude_job_service):
        self.project_service = project_service
        self.roadmap_service = roadmap_service
        self.claude_job_service = claude_job_service

    def register_routes(self, router):
        router.add("GET", "/p/{project_id}/api/roadmap", self.get_roadmap)
        router.add("POST", "/p/{project_id}/api/roadmap/approve", self.approve_roadmap)
        router.add("POST", "/p/{project_id}/api/roadmap/generate", self.generate_roadmap)
        router.add("POST", "/p/{project_id}/api/roadmap/refine", self.refine_roadmap)
        router.add("POST", "/p/{project_id}/api/codebase/map", self.map_codebase)
        router.add("GET", "/p/{project_id}/api/jobs", self.list_jobs)
        router.add("GET", "/p/{project_id}/api/jobs/{job_id}", self.get_job)

    def get_context(self, request):
        return self.project_service.get_project_context(request.route_params["project_id"])

    def get_roadmap(self, request):
        return respond_converting_errors(lambda: Response.json(200, self.roadmap_service.get_roadmap(self.get_context(request))))

    def approve_roadmap(self, request):
        return respond_converting_errors(lambda: Response.json(200, {"ok": True, "approval": self.roadmap_service.approve_roadmap(
            self.get_context(request), request.body.get("confirm"))}))

    def generate_roadmap(self, request):
        body = request.body
        return respond_converting_errors(lambda: self.respond_job(self.roadmap_service.generate_roadmap(
            self.get_context(request), body.get("goal"), map_first=body.get("map_first") is True,
            budget_usd=body.get("budget_usd"))))

    def refine_roadmap(self, request):
        body = request.body
        return respond_converting_errors(lambda: self.respond_job(self.roadmap_service.refine_roadmap(
            self.get_context(request), body.get("text"), budget_usd=body.get("budget_usd"))))

    def map_codebase(self, request):
        return respond_converting_errors(lambda: self.respond_job(self.roadmap_service.map_codebase(
            self.get_context(request), budget_usd=request.body.get("budget_usd"))))

    @staticmethod
    def respond_job(job):
        return Response.json(JOB_ACCEPTED_STATUS, {"ok": True, "job": job["id"], "detail": job})

    def list_jobs(self, request):
        return respond_converting_errors(lambda: Response.json(200, {"jobs": self.claude_job_service.list_jobs(
            self.get_context(request).repo_path)}))

    def get_job(self, request):
        return respond_converting_errors(lambda: Response.json(200, self.claude_job_service.get_job(
            self.get_context(request).repo_path, request.route_params["job_id"])))
