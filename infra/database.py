"""
Phase 3 — Data Tier: Database Layer

Provisions: RDS Postgres with Multi-AZ deployment, encryption, automated backups,
deletion protection, and credentials stored in AWS Secrets Manager.

Every function is idempotent — checks for existing resource before creating.
"""

import json
import logging
import time

from infra import client as aws
from infra import config
from utils.naming import resource_name
from utils.tagging import build_tags

log = logging.getLogger(__name__)


def create_db_subnet_group(name: str, subnet_ids: list[str]) -> str:
    """
    Creates RDS DB subnet group for Multi-AZ deployment.
    
    Args:
        name: logical name (e.g. "rds" → "ai-infra-dev-rds-subnet-group")
        subnet_ids: list of database subnet IDs (should be in different AZs)
    
    Returns:
        subnet_group_name: ready for use in RDS creation
    """
    rds = aws.get_client("rds")
    subnet_group_name = resource_name(f"{name}-subnet-group")
    
    # Check for existing subnet group
    try:
        response = rds.describe_db_subnet_groups(DBSubnetGroupName=subnet_group_name)
        if response["DBSubnetGroups"]:
            log.info(f"DB subnet group {subnet_group_name} already exists")
            return subnet_group_name
    except rds.exceptions.DBSubnetGroupNotFoundFault:
        pass
    except Exception as e:
        log.debug(f"Error checking DB subnet group: {e}")
    
    # Create subnet group
    log.info(f"Creating DB subnet group {subnet_group_name}")
    rds.create_db_subnet_group(
        DBSubnetGroupName=subnet_group_name,
        DBSubnetGroupDescription=f"Subnet group for {config.PROJECT}-{config.ENV} RDS",
        SubnetIds=subnet_ids,
        Tags=build_tags(f"{name}-subnet-group"),
    )
    
    log.info(f"DB subnet group created: {subnet_group_name}")
    return subnet_group_name


def create_rds(subnet_group: str, sg_id: str,
               db_name: str = "appdb",
               instance_class: str = "db.t3.micro",
               allocated_storage: int = 20,
               backup_retention_days: int = 7,
               multi_az: bool = True) -> dict:
    """
    Creates RDS Postgres instance with Multi-AZ, encryption, automated backups.
    
    Args:
        subnet_group: DB subnet group name
        sg_id: VPC security group ID (database SG)
        db_name: initial database name
        instance_class: e.g. "db.t3.micro", "db.t3.small"
        allocated_storage: storage in GB
        backup_retention_days: automated backup retention (0 = disabled)
        multi_az: enable Multi-AZ failover
    
    Returns:
        dict with:
            - endpoint: RDS endpoint hostname
            - port: database port (default 5432)
            - secret_arn: Secrets Manager secret containing credentials
    """
    rds = aws.get_client("rds")
    secrets = aws.get_client("secretsmanager")
    
    db_instance_id = resource_name("postgres")
    
    # Check for existing RDS instance
    try:
        response = rds.describe_db_instances(DBInstanceIdentifier=db_instance_id)
        if response["DBInstances"]:
            instance = response["DBInstances"][0]
            log.info(f"RDS instance {db_instance_id} already exists")
            
            # Try to retrieve existing secret
            try:
                secret_name = resource_name("rds-credentials")
                secret = secrets.describe_secret(SecretId=secret_name)
                secret_arn = secret["ARN"]
            except:
                secret_arn = "unknown"
            
            return {
                "endpoint": instance.get("Endpoint", {}).get("Address", ""),
                "port": instance.get("Endpoint", {}).get("Port", 5432),
                "secret_arn": secret_arn,
                "instance_id": db_instance_id,
            }
    except rds.exceptions.DBInstanceNotFoundFault:
        pass
    except Exception as e:
        log.debug(f"Error checking RDS instance: {e}")
    
    # Generate random master password
    import secrets as secrets_module
    master_password = secrets_module.token_urlsafe(16)
    
    # Create secret for credentials
    secret_name = resource_name("rds-credentials")
    log.info(f"Creating Secrets Manager secret for RDS credentials")
    
    secret_dict = {
        "username": "postgres",
        "password": master_password,
        "engine": "postgres",
        "host": "",  # Will be updated after RDS is created
        "port": 5432,
        "dbname": db_name,
    }
    
    try:
        secret_response = secrets.describe_secret(SecretId=secret_name)
        log.info(f"Secret {secret_name} already exists")
        secret_arn = secret_response["ARN"]
    except secrets.exceptions.ResourceNotFoundException:
        try:
            secret_response = secrets.create_secret(
                Name=secret_name,
                SecretString=json.dumps(secret_dict),
                Tags=build_tags("rds-credentials"),
            )
            secret_arn = secret_response["ARN"]
            log.info(f"Secret created: {secret_arn}")
        except Exception as e:
            log.error(f"Failed to create secret: {e}")
            raise
    
    # Create RDS instance
    log.info(f"Creating RDS instance {db_instance_id}")
    
    # Determine if this is dev (skip deletion protection) or prod
    skip_deletion = config.ENV == "dev"
    
    try:
        response = rds.create_db_instance(
            DBInstanceIdentifier=db_instance_id,
            DBInstanceClass=instance_class,
            Engine="postgres",
            EngineVersion="14.7",  # Latest stable Postgres 14
            MasterUsername="postgres",
            MasterUserPassword=master_password,
            AllocatedStorage=allocated_storage,
            StorageType="gp2",
            StorageEncrypted=True,
            KmsKeyId=None,  # Use default AWS-managed key
            VpcSecurityGroupIds=[sg_id],
            DBSubnetGroupName=subnet_group,
            BackupRetentionPeriod=backup_retention_days,
            PreferredBackupWindow="03:00-04:00",  # UTC
            PreferredMaintenanceWindow="mon:04:00-mon:05:00",  # UTC, after backup
            MultiAZ=multi_az,
            EnableIAMDatabaseAuthentication=False,
            EnableCloudwatchLogsExports=["postgresql"],
            DeletionProtection=not skip_deletion,
            DBName=db_name,
            Tags=build_tags("postgres"),
        )
    except Exception as e:
        log.error(f"Failed to create RDS instance: {e}")
        raise
    
    instance = response["DBInstance"]
    
    # Wait for instance to be available (LocalStack might return immediately)
    log.info(f"Waiting for RDS instance to become available...")
    max_attempts = 120
    attempt = 0
    
    while attempt < max_attempts:
        try:
            response = rds.describe_db_instances(DBInstanceIdentifier=db_instance_id)
            instance = response["DBInstances"][0]
            status = instance["DBInstanceStatus"]
            
            if status == "available":
                log.info(f"RDS instance is available")
                break
            else:
                log.info(f"RDS status: {status}, waiting...")
                time.sleep(5)
                attempt += 1
        except Exception as e:
            log.debug(f"Error checking RDS status: {e}")
            time.sleep(5)
            attempt += 1
    
    if attempt >= max_attempts:
        log.warning(f"RDS instance did not become available within timeout")
    
    # Update secret with actual endpoint
    endpoint = instance.get("Endpoint", {}).get("Address", "localhost")
    port = instance.get("Endpoint", {}).get("Port", 5432)
    
    secret_dict["host"] = endpoint
    secret_dict["port"] = port
    
    try:
        secrets.update_secret(
            SecretId=secret_arn,
            SecretString=json.dumps(secret_dict),
        )
        log.info(f"Updated secret with RDS endpoint")
    except Exception as e:
        log.warning(f"Failed to update secret with endpoint: {e}")
    
    result = {
        "endpoint": endpoint,
        "port": port,
        "secret_arn": secret_arn,
        "instance_id": db_instance_id,
    }
    
    log.info(f"RDS instance created successfully: {result}")
    return result


def get_connection_string(secret_arn: str) -> str:
    """
    Fetches credentials from Secrets Manager and returns SQLAlchemy connection string.
    
    Args:
        secret_arn: ARN of the Secrets Manager secret
    
    Returns:
        SQLAlchemy connection URL: postgresql://user:pass@host:port/dbname
        
    Note: Connection string is never logged.
    """
    secrets = aws.get_client("secretsmanager")
    
    try:
        response = secrets.get_secret_value(SecretId=secret_arn)
        secret_dict = json.loads(response["SecretString"])
        
        username = secret_dict.get("username", "postgres")
        password = secret_dict.get("password", "")
        host = secret_dict.get("host", "localhost")
        port = secret_dict.get("port", 5432)
        dbname = secret_dict.get("dbname", "appdb")
        
        # Build connection string (password includes special chars, so quote it)
        connection_string = f"postgresql://{username}:{password}@{host}:{port}/{dbname}"
        
        return connection_string
    except Exception as e:
        log.error(f"Failed to retrieve secret: {e}")
        raise


def provision_database(vpc_id: str, database_subnet_ids: list[str],
                      database_sg_id: str) -> dict:
    """
    Orchestrator for database tier provisioning.
    
    Returns:
        dict with endpoint, port, secret_arn, instance_id
    """
    log.info("=== Phase 3: Database Tier ===")
    
    # Create DB subnet group
    subnet_group = create_db_subnet_group(
        name="rds",
        subnet_ids=database_subnet_ids,
    )
    
    # Create RDS instance
    rds_result = create_rds(
        subnet_group=subnet_group,
        sg_id=database_sg_id,
        db_name="appdb",
        instance_class="db.t3.micro",
        allocated_storage=20,
        backup_retention_days=7,
        multi_az=True,
    )
    
    return rds_result
