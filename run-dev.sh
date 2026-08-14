#!/usr/bin/env bash
set -e

IMAGE_NAME="course-finder"
CONTAINER_NAME="course-finder-dev"
HOST_PORT="5001"
CONTAINER_PORT="5001"

echo "Stopping old container if it exists..."
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

echo "Building Docker image..."
docker build -t "$IMAGE_NAME" .

echo "Starting container..."

MSYS_NO_PATHCONV=1 docker run --rm \
  --name "$CONTAINER_NAME" \
  -p "$HOST_PORT:$CONTAINER_PORT" \
  -e PORT="$CONTAINER_PORT" \
  -v "$(pwd):/app" \
  -w /app \
  "$IMAGE_NAME" \
  bash -c "python build_course_index.py &&  python main.py"
