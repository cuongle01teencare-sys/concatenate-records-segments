#!/bin/sh
set -eu

# RUN_MODE is an image-level switch (NOT part of the app's own config system).
# It selects which subcommand this container runs:
#   RUN_MODE=concat  ->  python main.py --concat
#   RUN_MODE=sync    ->  python main.py --sync
#
# Deploy the SAME image as two Cloud Run jobs; set RUN_MODE=concat on one and
# RUN_MODE=sync on the other. Running a job then triggers its flag.
#
# Explicit container args override RUN_MODE (e.g. `docker run IMAGE --sync`),
# which is handy for local one-off runs.

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
    "")
        echo "RUN_MODE is not set. Set RUN_MODE=concat or RUN_MODE=sync (image-level env)," >&2
        echo "or pass an explicit flag as container args (e.g. --concat / --sync)." >&2
        exit 1
        ;;
    *)
        echo "Invalid RUN_MODE='${RUN_MODE}'. Expected 'concat' or 'sync'." >&2
        exit 1
        ;;
esac
