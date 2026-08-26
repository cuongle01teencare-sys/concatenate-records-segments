from google.cloud.storage.bucket import Bucket


def upload_from_string(
  bucket: Bucket,
  blob_name: str,
  data: str,
) -> None:
  if not isinstance(data, str):
    raise TypeError(
      "upload_from_string chỉ chấp nhận data dạng string."
    )

  blob = bucket.blob(blob_name)

  blob.upload_from_string(
    data,
    content_type="text/plain",
  )