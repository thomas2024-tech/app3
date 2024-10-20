import os
import yaml
import logging
import signal
import sys
from commlib.node import Node
from commlib.transports.redis import ConnectionParameters, Publisher
from commlib.pubsub import PubSubMessage
from commlib.rpc import RPCService, RPCMessage

# Configure logging to write to the console
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)

# Class for Version Messages (Publisher)
class VersionMessage(PubSubMessage):
    """Message format for version updates."""
    appname: str
    version_number: str
    dependencies: dict  # New field to store dependent apps and their versions

def load_docker_compose_data(file_path='docker-compose.yml'):
    """Reads appname and version_number from the image string in docker-compose.yml."""
    try:
        with open(file_path, 'r') as stream:
            compose_data = yaml.safe_load(stream)

        # Extract the first service name (for example, 'app1')
        service_name = list(compose_data['services'].keys())[0]

        # Extract the image field (for example, '1234a4321/app1:1.3')
        image = compose_data['services'][service_name]['image']

        # Parse the image to extract appname and version_number
        appname_with_repo = image.split('/')[1]  # 'app1:1.3'
        appname, version_number = appname_with_repo.split(':')  # Split to get 'app1' and '1.3'

        return appname, version_number
    except Exception as e:
        logging.error(f"Error reading docker-compose.yml: {e}")
        sys.exit(1)

def publish_version(channel, appname, version_number, redis_ip, dependencies=None):
    """Publishes a version message to a specified Redis channel."""
    # Define connection parameters for Redis
    redis_host = os.getenv('REDIS_HOST', redis_ip)
    redis_port = int(os.getenv('REDIS_PORT', 6379))  # Default Redis port
    redis_db = int(os.getenv('REDIS_DB', 0))  # Default Redis DB

    conn_params = ConnectionParameters(
        host=redis_host,
        port=redis_port,
        db=redis_db
    )

    # Initialize the Publisher with the specific channel/topic
    publisher = Publisher(
        conn_params=conn_params,
        topic=channel,
        msg_type=VersionMessage
    )

    # Create a VersionMessage, including the dependencies
    message = VersionMessage(appname=appname, version_number=version_number, dependencies=dependencies or {})

    # Publish the message
    publisher.publish(message)

    logging.info(f'Published version {version_number} of app {appname} to channel {channel}')
    if dependencies:
        for dep_app, dep_version in dependencies.items():
            logging.info(f'  Dependent app {dep_app} version {dep_version}')

# Class for handling RPC requests for Docker Compose commands
class DockerCommandRequest(RPCMessage):
    """RPC message for docker command requests."""
    directory: str

class DockerCommandResponse(RPCMessage):
    """RPC response for docker command execution."""
    success: bool
    message: str

class DockerComposeRPCService(RPCService):
    """RPC service to handle Docker Compose commands."""
    
    def handle(self, request: DockerCommandRequest) -> DockerCommandResponse:
        """Executes 'docker compose down' in the requested directory."""
        directory = request.directory
        try:
            # Execute the 'docker compose down' command in the specified directory
            result = os.system(f"docker compose -f {directory}/docker-compose.yml down")
            if result == 0:
                return DockerCommandResponse(success=True, message=f"'docker compose down' succeeded in {directory}")
            else:
                return DockerCommandResponse(success=False, message=f"Error running 'docker compose down' in {directory}")
        except Exception as e:
            return DockerCommandResponse(success=False, message=f"Exception occurred: {e}")

# Signal handler for graceful shutdown
def signal_handler(sig, frame):
    """Handles shutdown signals."""
    logging.info('Shutdown signal received. Exiting...')
    sys.exit(0)

if __name__ == "__main__":
    # Set up signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Load appname and version_number from docker-compose.yml
    appname, version_number = load_docker_compose_data()

    # Connection parameters for Redis
    redis_ip = os.getenv('REDIS_HOST', '192.168.1.52')

    # Initialize the RPC node
    node = Node(node_name='docker_rpc_server', connection_params=ConnectionParameters(host=redis_ip, port=6379))

    # Register the DockerComposeRPCService
    service = DockerComposeRPCService(node=node, rpc_name='docker_compose_service')

    # Example parameters for version info publishing
    channel = 'version_channel'
    dependencies = {
        'app1': '1.1',
        'app2': '1.1'
    }
    
    # Publish the version message (this can also be done periodically or based on events)
    publish_version(channel, appname, version_number, redis_ip, dependencies)

    # Start the RPC service (blocks forever)
    node.run_forever()
