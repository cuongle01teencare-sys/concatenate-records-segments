from pathlib import Path

from google.cloud.storage.bucket import Bucket

from .storage_utils import upload_from_string


def write_local_file(
  file_name: str,
  data: str,
) -> None:
  path = Path(file_name)

  path.parent.mkdir(
    parents=True,
    exist_ok=True,
  )

  path.write_text(
    data,
    encoding="utf-8",
  )


def write_log(
  *,
  environment: str,
  bucket: Bucket,
  path: str,
  data: str,
) -> None:
  if environment == "container":
    upload_from_string(
      bucket,
      path,
      data,
    )
    return

  write_local_file(
    path,
    data,
  )