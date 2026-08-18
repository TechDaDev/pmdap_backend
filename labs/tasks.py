from celery import shared_task
from django.conf import settings


@shared_task(
    bind=True,
    name="labs.extract_lab_results",
    soft_time_limit=settings.LAB_TASK_SOFT_TIME_LIMIT,
    time_limit=settings.LAB_TASK_TIME_LIMIT,
)
def extract_lab_results(self, document_uuid):
    """Structured lab extraction. Failure is non-fatal (recorded, not raised)."""
    from labs.services import process_lab_extraction

    return process_lab_extraction(document_uuid)
