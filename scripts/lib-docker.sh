#!/usr/bin/env bash
# container_ids PORT NAME -> ids of containers that publish PORT and run an image whose name contains NAME.
# Matching by port alone would stop whatever else a user runs on :8080; `docker ps` shows a digest-pinned image as a
# bare id, so the name comes from inspect.
container_ids() {
  local i
  for i in $(docker ps -q --filter "publish=$1" 2>/dev/null); do
    docker inspect -f '{{.Config.Image}}' "$i" 2>/dev/null | grep -qi -- "$2" && echo "$i"
  done
}
