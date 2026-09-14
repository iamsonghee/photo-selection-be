from fastapi import HTTPException


def require_owned_project(client, project_id, photographer_id, select: str = "id") -> dict:
    """project_id가 존재하고 photographer_id 소유인지 확인, 아니면 404."""
    r = (
        client.table("projects")
        .select(select)
        .eq("id", str(project_id))
        .eq("photographer_id", str(photographer_id))
        .limit(1)
        .execute()
    )
    if not r.data:
        raise HTTPException(status_code=404, detail="Project not found")
    return r.data[0]


def require_owned_job(client, job_id, photographer_id, select: str = "id,status,project_id") -> dict:
    """original_jobs 행을 조회하고(404), 그 project가 photographer_id 소유인지 확인한다(403)."""
    job_r = (
        client.table("original_jobs")
        .select(select)
        .eq("id", job_id)
        .limit(1)
        .execute()
    )
    if not job_r.data:
        raise HTTPException(status_code=404, detail="job not found")
    job = job_r.data[0]
    proj_r = (
        client.table("projects")
        .select("id")
        .eq("id", job["project_id"])
        .eq("photographer_id", str(photographer_id))
        .limit(1)
        .execute()
    )
    if not proj_r.data:
        raise HTTPException(status_code=403, detail="forbidden")
    return job
