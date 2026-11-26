from pydantic import ValidationError

from app.core.exceptions import RequestConflict
from app.repositories.videos import add_job
from app.schemas.video import GenerateVideo


def submit_video(db, tenant_id, request):
    with db.begin():
        job_id, stored_payload = add_job(db, tenant_id, request)
        try:
            canonical = GenerateVideo.model_validate(stored_payload).model_dump(mode="json")
        except ValidationError as exc:
            raise RequestConflict from exc
        if canonical != request.model_dump(mode="json"):
            raise RequestConflict
    return job_id

