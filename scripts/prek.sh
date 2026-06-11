#!/bin/sh

# In a jj workspace the working directory has no .git, so prek's internal "get git root" command
# exits with "fatal: not a git repository". Point GIT_DIR at the main repo's .git so prek works
# from the workspace.
if [ -f .jj/repo ]; then
    export GIT_DIR="$(cat .jj/repo)/../../.git"
fi

if command -v prek >/dev/null 2>&1; then
    exec prek "$@"
else
    exec uvx prek "$@"
fi
