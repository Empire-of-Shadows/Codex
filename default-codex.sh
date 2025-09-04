#!/bin/bash

# Discord Bot Deployment Script
set -e  # Exit on any error

# Configuration
CONTAINER_NAME="codex"
IMAGE_NAME="codex"
BACKUP_TAG="codex:backup"
HEALTH_CHECK_TIMEOUT=120  # seconds to wait for health check

echo "==== Starting Discord Bot Deployment ===="
echo "Timestamp: $(date)"

# Function to check container health
check_container_health() {
    echo "🏥 Checking container health..."

    local timeout=$HEALTH_CHECK_TIMEOUT
    local elapsed=0
    local interval=5

    while [ $elapsed -lt $timeout ]; do
        if docker inspect "$CONTAINER_NAME" --format='{{.State.Health.Status}}' 2>/dev/null | grep -q "healthy"; then
            echo "✅ Container is healthy!"
            return 0
        elif docker inspect "$CONTAINER_NAME" --format='{{.State.Health.Status}}' 2>/dev/null | grep -q "unhealthy"; then
            echo "❌ Container is unhealthy!"
            return 1
        else
            echo "⏳ Waiting for health check... (${elapsed}s/${timeout}s)"
            sleep $interval
            elapsed=$((elapsed + interval))
        fi
    done

    echo "⏰ Health check timeout reached"
    return 1
}

# Function to rollback to previous version
rollback() {
    echo "🔄 Rolling back to previous version..."

    # Stop current container
    docker compose down 2>/dev/null || true

    # Remove failed image
    docker rmi -f "$IMAGE_NAME" 2>/dev/null || true

    # Restore backup
    if docker images "$BACKUP_TAG" --format "{{.Repository}}:{{.Tag}}" | grep -q "$BACKUP_TAG"; then
        docker tag "$BACKUP_TAG" "$IMAGE_NAME"
        docker compose up -d

        # Wait for rollback to be healthy
        if check_container_health; then
            echo "✅ Rollback completed successfully"
        else
            echo "❌ Rollback failed - container is unhealthy"
            exit 1
        fi
    else
        echo "❌ No backup image found for rollback"
        exit 1
    fi
}

# Pre-deployment checks
echo "🔍 Running pre-deployment checks..."

# Check if docker and docker-compose are available
if ! command -v docker &> /dev/null; then
    echo "❌ Docker is not installed or not in PATH"
    exit 1
fi

if ! command -v docker compose &> /dev/null; then
    echo "❌ docker compose is not installed or not in PATH"
    exit 1
fi

# Check if .env file exists
if [ ! -f ".env" ]; then
    echo "❌ .env file not found"
    exit 1
fi

# Backup current image if it exists
if docker images "$IMAGE_NAME" --format "{{.Repository}}:{{.Tag}}" | grep -q "$IMAGE_NAME"; then
    echo "📦 Creating backup of current image..."
    docker tag "$IMAGE_NAME" "$BACKUP_TAG" || {
        echo "⚠️  Warning: Failed to create backup image"
    }
fi

# Step 1: Graceful shutdown with timeout
echo "🛑 Gracefully stopping container..."
if docker ps --filter "name=$CONTAINER_NAME" --format "{{.Names}}" | grep -q "$CONTAINER_NAME"; then
    # Send SIGTERM and wait
    docker compose down --timeout 30 || {
        echo "⚠️  Warning: Graceful shutdown failed, forcing stop..."
        docker kill "$CONTAINER_NAME" 2>/dev/null || true
        docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
    }
else
    echo "ℹ️  Container was not running"
fi

# Step 2: Clean up old image
echo "🧹 Cleaning up old image..."
docker rmi -f "$IMAGE_NAME" 2>/dev/null || echo "ℹ️  No old image to remove"

# Step 3: Build and start
echo "🏗️  Building new image and starting container..."
if docker compose up --build -d; then
    echo "🚀 Container started, waiting for health check..."

    # Wait for container to be healthy
    if check_container_health; then
        echo "✅ Deployment successful - container is healthy"

        # Clean up backup image after successful deployment
        docker rmi -f "$BACKUP_TAG" 2>/dev/null || true

        echo "==== Discord Bot Deployed Successfully! ===="
        echo "Timestamp: $(date)"
        echo ""
        echo "📋 Following logs (Press Ctrl+C to exit log view):"
        echo "================================================"

        # Follow logs
        docker logs -f "$CONTAINER_NAME"
    else
        echo "❌ Health check failed, initiating rollback..."
        rollback
        exit 1
    fi
else
    echo "❌ Failed to build/start container, initiating rollback..."
    rollback
    exit 1
fi
