import os

from dotenv import load_dotenv


load_dotenv(override=False)


VALID_ENVIRONMENTS = {
    "local",
    "container",
}


def get_bool_env(
    name: str,
    default: bool = False,
) -> bool:
    value = os.getenv(name)

    if value is None:
        return default

    return value.lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


PROJECT_ID = os.getenv(
    "PROJECT_ID",
    "tcservices2027",
)

BUCKET_NAME = os.getenv(
    "BUCKET_NAME",
    "meet-captures-2026",
)

ENVIRONMENT = os.getenv(
    "ENVIRONMENT",
    "local",
)

MATCH_GLOB = os.getenv(
    "MATCH_GLOB",
    "captures/*/*/participants/*/video/**/*.webm",
)

DATA_PROFILING = get_bool_env(
    "DATA_PROFILING",
    False,
)

# Giữ lại các WebM chunk thành phần đã được download vào temp
# sau khi FFmpeg ghép xong.
KEEP_ALL_WEBM_CHUNKS = get_bool_env(
    "KEEP_ALL_WEBM_CHUNKS",
    False,
)

# Giữ lại toàn bộ video WebM được ghép ra bằng cách lưu/upload
# file output sau khi FFmpeg ghép xong.
KEEP_ALL_CONCATENATED_WEBM_FILES = get_bool_env(
    "KEEP_ALL_CONCATENATED_WEBM_FILES",
    True,
)

# Giữ lại thư mục temp của student nếu quá trình ghép bị lỗi,
# để debug các chunk đã download, concat manifest, và file raw nếu có.
KEEP_TEMP_ON_FAILURE = get_bool_env(
    "KEEP_TEMP_ON_FAILURE",
    True,
)


if ENVIRONMENT not in VALID_ENVIRONMENTS:
    raise ValueError(
        "ENVIRONMENT chỉ chấp nhận "
        "'local' hoặc 'container'."
    )
