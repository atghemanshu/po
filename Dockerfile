# Dockerfile

# Stage 1: Base image with Python
FROM python:3.11-slim AS base
# Using python:3.11-slim as Render's build log showed Python 3.11.11 was used.
# Slim images are smaller.

# Set environment variables
ENV PYTHONUNBUFFERED 1
ENV APP_HOME /app
ENV LANG C.UTF-8
ENV LC_ALL C.UTF-8

WORKDIR $APP_HOME

# Install build tools and system dependencies including LibreOffice
# Using root to install packages (python:slim images start as root)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential \ # Good to have for any C extensions during pip install
    git \             # If any pip packages need to be installed from git
    libreoffice-writer \
    libreoffice-common \
    fonts-liberation \ # Good default fonts for document compatibility
    # xvfb \ # Optional: Sometimes needed for headless LibreOffice on some systems if direct headless fails
    # libreoffice-calc \ # If you ever need to process spreadsheets
    # libreoffice-impress # If you ever need to process presentations
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

# Expose the port Gunicorn will run on (Render typically uses 10000 for Docker services)
EXPOSE 10000

# Command to run your application using Gunicorn
# Using 0.0.0.0 makes the app accessible from outside the container (within Render's network)
# Increased timeout for potentially long file processing (OCR, PDF generation)
# --workers 1: Important for free tiers and resource management, especially with CloudConvert fallback
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:10000", "--timeout", "120", "--workers", "1", "--log-level", "info"]