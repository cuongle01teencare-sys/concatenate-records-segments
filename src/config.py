import os

from dotenv import load_dotenv


load_dotenv(override=False, dotenv_path=".env")


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

# Keep downloaded source WebM chunks in temp after FFmpeg finishes.
KEEP_ALL_WEBM_CHUNKS = get_bool_env(
    "KEEP_ALL_WEBM_CHUNKS",
    False,
)

# Keep concatenated WebM files on temp disk after they have been saved/uploaded.
# false still saves/uploads the output, then removes the temp output file.
KEEP_ALL_CONCATENATED_WEBM_FILES = get_bool_env(
    "KEEP_ALL_CONCATENATED_WEBM_FILES",
    True,
)

# Keep temp directories when concatenation fails, for debugging downloaded
# chunks, concat manifests, and raw byte-concat files.
KEEP_TEMP_ON_FAILURE = get_bool_env(
    "KEEP_TEMP_ON_FAILURE",
    True,
)

SUPABASE_API_URL = os.getenv("SUPABASE_API_URL", "")

SUPABASE_API_KEY = os.getenv("SUPABASE_API_KEY", "")

# Blob glob used by the `--sync` command to locate session-level concatenated
# WebM outputs whose record link should be synced into Supabase.
SUPABASE_SYNC_MATCH_GLOB = os.getenv(
    "SUPABASE_SYNC_MATCH_GLOB",
    "captures/*/*/concatenated/*.webm",
)

if ENVIRONMENT not in VALID_ENVIRONMENTS:
    raise ValueError(
        "ENVIRONMENT only accepts 'local' or 'container'."
    )
