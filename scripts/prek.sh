#!/bin/sh

if [ ! -e .git ]; then
    printf >&2 "not a git repo: cannot run prek\n"
    exit 0

fi

if command -v prek >/dev/null 2>&1; then
    exec prek "$@"
else
    exec uvx prek "$@"
fi
