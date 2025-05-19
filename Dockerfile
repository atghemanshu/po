# Use an official Python image as a parent image.
# Using a specific version like 3.11 is good for consistency.
# The "-slim" variants are smaller.
FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED 1                     # Ensures print statements and log messages appear immediately
ENV APP_HOME /app                          # Define a working directory for your app inside the container
WORKDIR $APP_HOME

# Install system dependencies:
# - git: Might be needed if pip installs any packages from git repositories (less common for your list)
# - libreoffice-writer: Provides 'soffice' for DOCX conversion
# - libreoffice-common: Common files for LibreOffice
# - fonts-liberation: Good default fonts for document compatibility
# Using root to install packages. The python:slim image starts as root.
RUN apt-get update && \
    apt-get install -y \
    git \
    libreoffice-writer \
    libreoffice-common \
    fonts-liberation \
    --no-install-recommends && \
    # Clean up apt caches to reduce image size
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Copy the requirements file into the container
COPY requirements.txt .

# Install Python dependencies
# Using --no-cache-dir can reduce image size slightly
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code into the container
COPY . .

# Define the command to run your application using Gunicorn
# Render will typically set the PORT environment variable, which Gunicorn can use.
# If not, Render maps its internal port 10000 (or another) to your service's external port.
# Using 0.0.0.0 makes the app accessible from outside the container.
# Increased timeout for potentially long file processing.
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:10000", "--timeout", "120", "--workers", "1"]