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
from commlib.rpc import BaseRPCService, RPCMessage

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

# VersionMessage class
class VersionMessage(PubSubMessage):
    appname: str
    version_number: str
    dependencies: dict

# RPC message classes
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

class DockerComposeRPCService(BaseRPCService):
    """RPC service to handle Docker Compose commands."""
    
    def __init__(self, node: Node, rpc_name: str):
        self._node = node
        self.msg_type = DockerCommandRequest
        self.resp_type = DockerCommandResponse
        super().__init__(
            msg_type=DockerCommandRequest,
            rpc_name=rpc_name
        )
    
    def run(self):
        """Run the service."""
        while True:
            try:
                self.process_next_message()
            except Exception as e:
                logging.error(f"Error processing message: {e}")
    
    def handle_message(self, message: DockerCommandRequest) -> DockerCommandResponse:
        """Handle incoming RPC messages."""
        command = message.command
        directory = message.directory
        docker_compose_file = os.path.join(directory, 'docker-compose.yml')
        
        if command == 'down':
            try:
                result = subprocess.run(
                    ["docker-compose", "-f", docker_compose_file, "down"],
                    capture_output=True,
                    text=True
                )
                if result.returncode == 0:
                    return DockerCommandResponse(success=True, message=f"'docker-compose down' succeeded in {directory}")
                else:
                    return DockerCommandResponse(success=False, message=f"Error: {result.stderr}")
            except Exception as e:
                return DockerCommandResponse(success=False, message=f"Exception occurred: {e}")
        
        elif command == 'update_version':
            new_version = message.new_version
            try:
                with open(docker_compose_file, 'r') as file:
                    compose_data = yaml.safe_load(file)
                service_name = list(compose_data['services'].keys())[0]
                image = compose_data['services'][service_name]['image']
                repo, appname_with_version = image.split('/')
                appname, current_version = appname_with_version.split(':')
                new_image = f"{repo}/{appname}:{new_version}"
                compose_data['services'][service_name]['image'] = new_image
                
                with open(docker_compose_file, 'w') as file:
                    yaml.dump(compose_data, file)
                
                subprocess.run(
                    ["docker-compose", "-f", docker_compose_file, "down"],
                    check=True
                )
                subprocess.run(
                    ["docker-compose", "-f", docker_compose_file, "up", "-d"],
                    check=True
                )
                return DockerCommandResponse(success=True, message=f"Updated {appname} to version {new_version}")
            except subprocess.CalledProcessError as e:
                return DockerCommandResponse(success=False, message=f"Subprocess error: {e}")
            except Exception as e:
                return DockerCommandResponse(success=False, message=f"Exception occurred: {e}")
        
        else:
            return DockerCommandResponse(success=False, message=f"Unknown command '{command}'")

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

    # Load appname and version_number from docker-compose.yml
    appname, version_number = load_docker_compose_data(directory=script_dir)

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

        # Initialize the Node
        node = Node(
            node_name='docker_rpc_server_machine1',
            connection_params=conn_params
        )

        # Create and start node thread
        import threading
        node_thread = threading.Thread(target=node.run, daemon=True)
        node_thread.start()

        # Create the RPC service
        service = DockerComposeRPCService(
            node=node,
            rpc_name='docker_compose_service_machine3'
        )

        # Example parameters for version info publishing
        channel = 'version_channel'
        dependencies = {
            'app1': '1.1',
            'app2': '1.1'
        }

        # Publish the version message
        publish_version(channel, appname, version_number, redis_ip, dependencies)

        # Run the service
        service.run()

    except Exception as e:
        logging.error(f"Error starting service: {e}")
        sys.exit(1)