import os
import yaml
import logging
import signal
import sys
import subprocess
from dotenv import load_dotenv
from commlib.node import Node
from commlib.transports.redis import ConnectionParameters
from commlib.pubsub import PubSubMessage
from commlib.rpc import RPCMessage  # Note we import RPCMessage directly now
import time
import threading
import docker

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

# VersionMessage class for publish/subscribe communications
class VersionMessage(PubSubMessage):
    appname: str
    version_number: str
    dependencies: dict

# RPC message classes - now properly inheriting from RPCMessage base classes
class DockerCommandRequest(RPCMessage):
    command: str
    directory: str
    new_version: str = None

class DockerCommandResponse(RPCMessage):
    success: bool
    message: str

def load_docker_compose_data(directory='.', filename='docker-compose.yml'):
    """Reads appname and version_number from the image string in docker-compose.yml."""
    file_path = os.path.join(os.path.abspath(directory), filename)
    logging.info(f"Looking for docker-compose file at: {file_path}")
    
    try:
        with open(file_path, 'r') as stream:
            compose_data = yaml.safe_load(stream)
            if not compose_data or 'services' not in compose_data:
                raise ValueError(f"Invalid docker-compose file: {file_path}")
            
            service_name = list(compose_data['services'].keys())[0]
            image = compose_data['services'][service_name]['image']
            if '/' not in image or ':' not in image:
                raise ValueError(f"Invalid image format in {file_path}: {image}")
                
            appname_with_repo = image.split('/')[1]
            appname, version_number = appname_with_repo.split(':')
            return appname, version_number
    except FileNotFoundError:
        logging.error(f"Docker compose file not found: {file_path}")
        sys.exit(1)
    except Exception as e:
        logging.error(f"Error reading {file_path}: {e}")
        sys.exit(1)

def publish_version(channel, appname, version_number, redis_ip, dependencies=None):
    """Publishes a version message to a specified Redis channel."""
    redis_host = os.getenv('REDIS_HOST', redis_ip)
    redis_port = int(os.getenv('REDIS_PORT', 6379))
    redis_db = int(os.getenv('REDIS_DB', 0))

    conn_params = ConnectionParameters(
        host=redis_host,
        port=redis_port,
        db=redis_db
    )

    from commlib.transports.redis import Publisher
    publisher = Publisher(
        conn_params=conn_params,
        topic=channel,
        msg_type=VersionMessage
    )

    message = VersionMessage(appname=appname, version_number=version_number, dependencies=dependencies or {})
    publisher.publish(message)

    logging.info(f'Published version {version_number} of app {appname} to channel {channel}')
    if dependencies:
        for dep_app, dep_version in dependencies.items():
            logging.info(f'  Dependent app {dep_app} version {dep_version}')

def process_request(message):
    try:
        logging.info(f"⭐ Received update request: {message}")
        
        client = docker.from_env()
        container_directory = '/app/host_dir'
        new_version = message.get('new_version')
        
        # Read existing compose file
        docker_compose_file = os.path.join(container_directory, 'docker-compose.yml')
        with open(docker_compose_file, 'r') as file:
            compose_data = yaml.safe_load(file)
        
        # Create new compose data with version
        new_compose_data = compose_data.copy()
        service_name = list(new_compose_data['services'].keys())[0]
        current_image = new_compose_data['services'][service_name]['image']
        repo = current_image.rsplit(':', 1)[0]
        new_image = f"{repo}:{new_version}"

        # Create new service name with version
        versioned_service_name = f"{service_name}_{new_version.replace('.', '_')}"

        # Copy service config to new name and delete old one
        new_compose_data['services'][versioned_service_name] = new_compose_data['services'][service_name].copy()
        del new_compose_data['services'][service_name]

        # Update image in new service
        new_compose_data['services'][versioned_service_name]['image'] = new_image
        
        # Write the new compose file
        new_compose_file = os.path.join(container_directory, f'docker-compose-version{new_version.replace(".", "_")}.yml')
        with open(new_compose_file, 'w') as file:
            yaml.dump(new_compose_data, file, default_flow_style=False, sort_keys=False)

        # Ensure network exists
        network_name = 'app_network'
        try:
            network = client.networks.get(network_name)
        except docker.errors.NotFound:
            network = client.networks.create(network_name, driver='bridge')

        # Get container configuration and environment
        container_config = new_compose_data['services'][versioned_service_name]  # Use new service name
        environment = container_config.get('environment', {})
        
        # Convert environment list to dictionary if needed
        if isinstance(environment, list):
            env_dict = {}
            for item in environment:
                if isinstance(item, str) and '=' in item:
                    key, value = item.split('=', 1)
                    env_dict[key] = value
            environment = env_dict

        # Make sure REDIS_HOST is set
        if 'REDIS_HOST' in environment and '${REDIS_HOST}' in environment['REDIS_HOST']:
            environment['REDIS_HOST'] = os.getenv('REDIS_HOST', 'localhost')

        logging.info("Creating new container...")
        try:
            # Start new container first
            new_container = client.containers.run(
                image=new_image,
                detach=True,
                name=f"{versioned_service_name}".replace('.', '_'),  # Use new service name
                volumes=container_config.get('volumes', []),
                environment=environment,
                working_dir=container_config.get('working_dir'),
                privileged=True,
                network=network_name,
                restart_policy={"Name": "unless-stopped"}
            )
            
            # Wait and check if container is running
            time.sleep(5)
            new_container.reload()
            if new_container.status != 'running':
                logs = new_container.logs().decode('utf-8')
                raise Exception(f"Container failed to start. Logs: {logs}")

            # Schedule shutdown of current container after response is sent
            def delayed_shutdown():
                time.sleep(2)  # Small delay to ensure response is sent
                for container in client.containers.list(all=True):
                    if service_name in container.name and new_version not in container.name:
                        logging.info(f"Stopping old container {container.name}")
                        container.stop(timeout=10)
                        container.remove()

            import threading
            threading.Thread(target=delayed_shutdown, daemon=True).start()

            return {
                'success': True,
                'message': f"Successfully started version {new_version}, shutting down old version"
            }

        except Exception as e:
            logging.error(f"Container start failed: {e}")
            return {
                'success': False,
                'message': f"Container start failed: {e}"
            }

    except Exception as e:
        error_msg = f"Update failed: {str(e)}"
        logging.error(error_msg)
        return {
            'success': False,
            'message': error_msg
        }
    
def signal_handler(sig, frame):
    """Handles shutdown signals."""
    logging.info('Shutdown signal received. Exiting...')
    sys.exit(0)

if __name__ == "__main__":
    # Log the loaded environment variables for debugging
    redis_ip = os.getenv('REDIS_HOST')
    logging.info(f"Loaded REDIS_HOST from environment: {redis_ip}")
    
    # Set up signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Get the directory containing the script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    logging.info(f"Script directory: {script_dir}")

    # Load appname from docker-compose.yml and version from current container
    appname, _ = load_docker_compose_data(directory=script_dir)  # Get appname only

    # Check Redis host
    if not redis_ip:
        logging.error("REDIS_HOST environment variable is not set.")
        sys.exit(1)

    try:
        # Create connection parameters
        conn_params = ConnectionParameters(
            host=redis_ip,
            port=int(os.getenv('REDIS_PORT', 6379)),
            db=int(os.getenv('REDIS_DB', 0))
        )

        # Create the Node
        node = Node(
            node_name='docker_rpc_server_machine1',
            connection_params=conn_params
        )

        # Create RPC service with explicit message types
        service = node.create_rpc(
            rpc_name='docker_compose_service_machine1',
            on_request=process_request
        )

        # Start the node in a background thread
        node_thread = threading.Thread(target=node.run, daemon=True)
        node_thread.start()

        # Define dependencies, channel and version number
        channel = 'version_channel'
        version_number = "1.1"
        dependencies = {
            'app1': '1.1',
            'app2': '1.1'
        }

        # First publish version to establish presence
        publish_version(channel, appname, version_number, redis_ip, dependencies)

        # Set up and start periodic version publishing
        def publish_version_periodically():
            while True:
                try:
                    publish_version(channel, appname, version_number, redis_ip, dependencies)
                    time.sleep(60)
                except Exception as e:
                    logging.error(f"Error publishing version: {e}")
                    time.sleep(5)

        publisher_thread = threading.Thread(target=publish_version_periodically, daemon=True)
        publisher_thread.start()

        while True:
            time.sleep(0.1)
            
    except KeyboardInterrupt:
        logging.info("Received keyboard interrupt, shutting down...")
    except Exception as e:
        logging.error(f"Error starting service: {e}")
    finally:
        logging.info("Shutting down services...")
        node.stop()
        sys.exit(0)