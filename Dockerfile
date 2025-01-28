# Python 3.9 runtime as parent image
FROM python:3.9-slim

# Install only essential build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory in the container
WORKDIR /app

# Copy only essential requirements
COPY requirements.txt ./

# Install Python packages
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY . .

# Expose port 80
EXPOSE 80

# Run the application
CMD ["python", "main.py"]
