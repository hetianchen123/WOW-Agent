#!/bin/zsh
set -eu
exec "$(dirname "$0")/quest-accept.command" navigate-quest "$@"
