#!/bin/bash

# Ensure script is run as root
if [ "$EUID" -ne 0 ]; then
  echo "Error: Please run this script as root (or using sudo)."
  exit 1
fi

echo "----------------------------------------"
echo "Checking for Docker installation..."

# Check if docker command exists
if command -v docker &> /dev/null; then
    echo "✅ Docker is already installed on this system."
    docker --version
else
    echo "⚠️ Docker is not installed. Starting installation for AlmaLinux..."
    
    # 1. Update system packages
    echo "Updating system..."
    dnf update -y
    
    # 2. Install required dependencies
    echo "Installing yum-utils..."
    dnf install -y yum-utils
    
    # 3. Add the official Docker CE repository (AlmaLinux uses CentOS repos)
    echo "Adding Docker repository..."
    yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
    
    # 4. Install Docker Engine, CLI, Containerd, and Compose plugin
    echo "Installing Docker packages..."
    dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    
    # 5. Start and enable Docker to run on boot
    echo "Starting and enabling Docker service..."
    systemctl start docker
    systemctl enable docker
    
    echo "✅ Docker installation completed successfully!"
    docker --version
fi

echo "----------------------------------------"
echo "Checking for docker-compose..."

# Check if docker-compose (standalone) is available, as old scripts might use `docker-compose` instead of `docker compose`
if ! command -v docker-compose &> /dev/null; then
    echo "⚠️ docker-compose (standalone) not found. Installing..."
    curl -SL "https://github.com/docker/compose/releases/download/v2.27.0/docker-compose-linux-x86_64" -o /usr/local/bin/docker-compose
    chmod +x /usr/local/bin/docker-compose
    
    # Create symlink in /usr/bin just in case
    ln -s /usr/local/bin/docker-compose /usr/bin/docker-compose 2>/dev/null
    
    echo "✅ docker-compose installed!"
else
    echo "✅ docker-compose is already installed."
    docker-compose --version
fi

echo "----------------------------------------"
echo "Setting up environment configuration..."
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        echo "✅ Created .env file from .env.example"
    elif [ -f env.example ]; then
        cp env.example .env
        echo "✅ Created .env file from env.example"
    else
        echo "⚠️ Neither .env.example nor env.example found, could not create .env"
    fi
else
    echo "✅ .env file already exists, skipping copy."
fi

echo "----------------------------------------"
echo "Setup Complete! You can now run your project using 'docker-compose up --build -d'."
