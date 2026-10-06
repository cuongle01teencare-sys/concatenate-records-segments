#!/bin/sh
set -eu

# RUN_MODE is an image-level switch (NOT part of the app's own config system).
# It selects what this container does:
#   RUN_MODE=concat  ->  python main.py --concat        (batch job)
#   RUN_MODE=sync    ->  python main.py --sync          (batch job)
#   RUN_MODE=concat-audio -> python main.py --concat-audio (batch job)
#   RUN_MODE=serve   ->  functions-framework HTTP server (Cloud Run Service /
#                        Cloud Function). Nhận POST với payload glob pattern.
#
# Batch: deploy image làm Cloud Run Job, đặt RUN_MODE=concat hoặc =sync.
# Service: deploy image làm Cloud Run Service, đặt RUN_MODE=serve. Cloud Run
# tự set biến $PORT.
#
# Explicit container args override RUN_MODE (e.g. `docker run IMAGE --sync`),
# tiện cho các lần chạy one-off local.

if [ "$#" -gt 0 ]; then
    exec python main.py "$@"
fi

case "${RUN_MODE:-}" in
    concat)
        exec python main.py --concat
        ;;
    sync)
        exec python main.py --sync
        ;;
    concat-audio)
        exec python main.py --concat-audio
        ;;
    serve)
        exec functions-framework \
            --target=handle \
            --source=function_main.py \
            --host=0.0.0.0 \
            --port="${PORT:-8080}"
        ;;
    "")
        echo "RUN_MODE is not set. Set RUN_MODE=concat | sync | serve (image-level env)," >&2
        echo "or pass an explicit flag as container args (e.g. --concat / --sync)." >&2
        exit 1
        ;;
    *)
        echo "Invalid RUN_MODE='${RUN_MODE}'. Expected 'concat', 'sync', 'concat-audio' or 'serve'." >&2
        exit 1
        ;;
esac
