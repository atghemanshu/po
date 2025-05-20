# Dockerfile

# Stage 1: Base image with Python
FROM python:3.11-slim AS base

# Set environment variables
ENV PYTHONUNBUFFERED 1
ENV APP_HOME /app
ENV LANG C.UTF-8
ENV LC_ALL C.UTF-8

WORKDIR $APP_HOME

# Install system dependencies:
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential \
    git \
    libreoffice-writer \
    libreoffice-common \
    fonts-liberation \
    && \
    # Clean up apt caches to reduce image size
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Copy only requirements.txt first to leverage Docker cache
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code
COPY . .

# Expose the port Gunicorn will run on
EXPOSE 10000

# Command to run your application using Gunicorn
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:10000", "--timeout", "120", "--workers", "1", "--log-level", "info"]