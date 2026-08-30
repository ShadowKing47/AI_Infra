"""
Phase 6 — Monitoring & Observability

Provisions: CloudWatch alarms, dashboards, and custom metric publishing.
Used by predictive scaler and drift detection.

Every function is idempotent — checks for existing resource before creating.
"""

import json
import logging

from infra import client as aws
from infra import config
from utils.naming import resource_name
from utils.tagging import build_tags

log = logging.getLogger(__name__)


def _is_resource_not_found(e: Exception) -> bool:
    """Check if exception is a 'ResourceNotFound' type error."""
    return "ResourceNotFound" in type(e).__name__ or "NotFound" in str(e)


def create_alarm(name: str, namespace: str, metric: str,
                 threshold: float, comparison: str,
                 alarm_actions: list[str] = None,
                 ok_actions: list[str] = None,
                 dimensions: dict = None,
                 evaluation_periods: int = 2,
                 datapoints_to_alarm: int = 2,
                 period: int = 300,
                 statistic: str = "Average",
                 treat_missing_data: str = "missing") -> str:
    """
    Creates CloudWatch alarm.
    
    Args:
        name: logical name for the alarm
        namespace: CloudWatch namespace (e.g., "AWS/ApplicationELB")
        metric: metric name (e.g., "RequestCount")
        threshold: alarm threshold
        comparison: comparison operator ("GreaterThanThreshold", "LessThanThreshold", etc.)
        alarm_actions: SNS topic ARNs for alarm state
        ok_actions: SNS topic ARNs for OK state
        dimensions: metric dimensions dict
        evaluation_periods: number of periods to evaluate
        datapoints_to_alarm: datapoints needed to trigger alarm
        period: period in seconds
        statistic: statistic to apply
        treat_missing_data: how to treat missing data
        
    Returns:
        alarm_arn
    """
    cw = aws.get_client("cloudwatch")
    alarm_name = resource_name(f"alarm-{name}")
    
    # Check for existing alarm
    try:
        response = cw.describe_alarms(AlarmNames=[alarm_name])
        if response["MetricAlarms"]:
            alarm = response["MetricAlarms"][0]
            log.info(f"Alarm {alarm_name} already exists: {alarm['AlarmArn']}")
            return alarm["AlarmArn"]
    except Exception as e:
        if _is_resource_not_found(e):
            pass
        else:
            log.debug(f"Error checking alarm: {e}")
    
    # Build alarm configuration
    alarm_config = {
        "AlarmName": alarm_name,
        "AlarmDescription": f"Alarm for {namespace}/{metric}",
        "Namespace": namespace,
        "MetricName": metric,
        "Dimensions": [{"Name": k, "Value": v} for k, v in (dimensions or {}).items()],
        "Statistic": statistic,
        "Period": period,
        "EvaluationPeriods": evaluation_periods,
        "DatapointsToAlarm": datapoints_to_alarm,
        "Threshold": threshold,
        "ComparisonOperator": comparison,
        "TreatMissingData": treat_missing_data,
        "Tags": build_tags(f"alarm-{name}"),
    }
    
    if alarm_actions:
        alarm_config["AlarmActions"] = alarm_actions
    if ok_actions:
        alarm_config["OKActions"] = ok_actions
    
    log.info(f"Creating CloudWatch alarm: {alarm_name}")
    try:
        response = cw.put_metric_alarm(**alarm_config)
        log.info(f"Alarm created successfully")
    except Exception as e:
        log.error(f"Failed to create alarm: {e}")
        raise
    
    # Get ARN
    response = cw.describe_alarms(AlarmNames=[alarm_name])
    alarm_arn = response["MetricAlarms"][0]["AlarmArn"]
    
    return alarm_arn


def create_dashboard(name: str, widgets: list[dict]) -> str:
    """
    Creates CloudWatch dashboard.
    
    Args:
        name: logical name for the dashboard
        widgets: list of widget definitions
        
    Returns:
        dashboard_arn
    """
    cw = aws.get_client("cloudwatch")
    dashboard_name = resource_name(f"dashboard-{name}")
    
    # Check for existing dashboard
    try:
        response = cw.get_dashboard(DashboardName=dashboard_name)
        log.info(f"Dashboard {dashboard_name} already exists")
        return response["DashboardArn"]
    except Exception as e:
        if _is_resource_not_found(e):
            pass
        else:
            log.debug(f"Error checking dashboard: {e}")
    
    # Create dashboard body
    dashboard_body = {
        "widgets": widgets,
    }
    
    log.info(f"Creating CloudWatch dashboard: {dashboard_name}")
    try:
        response = cw.put_dashboard(
            DashboardName=dashboard_name,
            DashboardBody=json.dumps(dashboard_body),
        )
        log.info(f"Dashboard created successfully")
        return response["DashboardArn"]
    except Exception as e:
        log.error(f"Failed to create dashboard: {e}")
        raise


def put_metric(namespace: str, metric_name: str, value: float,
               dimensions: dict = None, unit: str = "None",
               timestamp=None) -> None:
    """
    Publishes a custom metric datapoint.
    
    Args:
        namespace: CloudWatch namespace (e.g., "MLOps/Scaler")
        metric_name: metric name
        value: metric value
        dimensions: metric dimensions dict
        unit: CloudWatch unit
        timestamp: datetime (default: now)
    """
    cw = aws.get_client("cloudwatch")
    
    metric_data = {
        "MetricName": metric_name,
        "Value": value,
        "Unit": unit,
    }
    
    if dimensions:
        metric_data["Dimensions"] = [{"Name": k, "Value": v} for k, v in dimensions.items()]
    
    if timestamp:
        metric_data["Timestamp"] = timestamp
    
    try:
        cw.put_metric_data(
            Namespace=namespace,
            MetricData=[metric_data],
        )
        log.debug(f"Published metric {namespace}/{metric_name} = {value}")
    except Exception as e:
        log.error(f"Failed to publish metric: {e}")
        raise


def create_mlops_dashboard() -> str:
    """Creates standard MLOps dashboard with key widgets."""
    
    widgets = [
        # ALB Request Count
        {
            "type": "metric",
            "x": 0, "y": 0, "width": 12, "height": 6,
            "properties": {
                "metrics": [
                    ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", resource_name("alb")],
                    [".", "TargetResponseTime", ".", "."],
                    [".", "HTTPCode_Target_2XX_Count", ".", "."],
                    [".", "HTTPCode_Target_5XX_Count", ".", "."],
                ],
                "period": 300,
                "stat": "Sum",
                "region": config.REGION,
                "title": "ALB Metrics",
            }
        },
        # ASG Metrics
        {
            "type": "metric",
            "x": 12, "y": 0, "width": 12, "height": 6,
            "properties": {
                "metrics": [
                    ["AWS/AutoScaling", "GroupDesiredCapacity", "AutoScalingGroupName", resource_name("ml-asg")],
                    [".", "GroupInServiceInstances", ".", "."],
                    [".", "GroupPendingInstances", ".", "."],
                    [".", "GroupTerminatingInstances", ".", "."],
                ],
                "period": 300,
                "stat": "Average",
                "region": config.REGION,
                "title": "ML ASG Metrics",
            }
        },
        # RDS Metrics
        {
            "type": "metric",
            "x": 0, "y": 6, "width": 12, "height": 6,
            "properties": {
                "metrics": [
                    ["AWS/RDS", "CPUUtilization", "DBInstanceIdentifier", resource_name("postgres")],
                    [".", "DatabaseConnections", ".", "."],
                    [".", "FreeableMemory", ".", "."],
                    [".", "ReadLatency", ".", "."],
                ],
                "period": 300,
                "stat": "Average",
                "region": config.REGION,
                "title": "RDS Metrics",
            }
        },
        # ElastiCache Metrics
        {
            "type": "metric",
            "x": 12, "y": 6, "width": 12, "height": 6,
            "properties": {
                "metrics": [
                    ["AWS/ElastiCache", "CPUUtilization", "ReplicationGroupId", resource_name("redis")],
                    [".", "DatabaseMemoryUsagePercentage", ".", "."],
                    [".", "CurrConnections", ".", "."],
                    [".", "CacheHits", ".", "."],
                ],
                "period": 300,
                "stat": "Average",
                "region": config.REGION,
                "title": "Redis Metrics",
            }
        },
        # MLOps Custom Metrics
        {
            "type": "metric",
            "x": 0, "y": 12, "width": 24, "height": 6,
            "properties": {
                "metrics": [
                    ["MLOps/Scaler", "ForecastedLoad", "ASG", resource_name("ml-asg")],
                    ["MLOps/Scaler", "ScalingActionScheduled", "ASG", resource_name("ml-asg")],
                    ["MLOps/DriftMonitor", "FeatureDriftPSI", "Model", "sentiment"],
                    ["MLOps/DriftMonitor", "LabelDriftPSI", "Model", "sentiment"],
                ],
                "period": 300,
                "stat": "Average",
                "region": config.REGION,
                "title": "MLOps Custom Metrics",
            }
        },
    ]
    
    return create_dashboard("mlops", widgets)


def create_standard_alarms(alb_arn: str, ml_asg_name: str, rds_instance_id: str,
                          redis_replication_group_id: str, sns_topic_arn: str = None) -> dict:
    """
    Creates standard set of alarms for the platform.
    
    Args:
        alb_arn: ALB ARN
        ml_asg_name: ML ASG name
        rds_instance_id: RDS instance ID
        redis_replication_group_id: Redis replication group ID
        sns_topic_arn: Optional SNS topic for alarm notifications
        
    Returns:
        dict of alarm names to ARNs
    """
    alarm_actions = [sns_topic_arn] if sns_topic_arn else []
    
    alarms = {}
    
    # ALB alarms
    alb_name = alb_arn.split("/")[-2] + "/" + alb_arn.split("/")[-1]
    
    alarms["alb_high_latency"] = create_alarm(
        name="alb-high-latency",
        namespace="AWS/ApplicationELB",
        metric="TargetResponseTime",
        threshold=2.0,
        comparison="GreaterThanThreshold",
        alarm_actions=alarm_actions,
        dimensions={"LoadBalancer": alb_name},
        evaluation_periods=3,
        period=60,
        statistic="Average",
    )
    
    alarms["alb_5xx_errors"] = create_alarm(
        name="alb-5xx-errors",
        namespace="AWS/ApplicationELB",
        metric="HTTPCode_Target_5XX_Count",
        threshold=10,
        comparison="GreaterThanThreshold",
        alarm_actions=alarm_actions,
        dimensions={"LoadBalancer": alb_name},
        evaluation_periods=2,
        period=60,
        statistic="Sum",
    )
    
    alarms["alb_unhealthy_hosts"] = create_alarm(
        name="alb-unhealthy-hosts",
        namespace="AWS/ApplicationELB",
        metric="UnHealthyHostCount",
        threshold=0,
        comparison="GreaterThanThreshold",
        alarm_actions=alarm_actions,
        dimensions={"LoadBalancer": alb_name, "TargetGroup": resource_name("tg-ml")},
        evaluation_periods=2,
        period=60,
        statistic="Maximum",
    )
    
    # ML ASG alarms
    alarms["ml_asg_capacity"] = create_alarm(
        name="ml-asg-capacity",
        namespace="AWS/AutoScaling",
        metric="GroupInServiceInstances",
        threshold=1,
        comparison="LessThanThreshold",
        alarm_actions=alarm_actions,
        dimensions={"AutoScalingGroupName": ml_asg_name},
        evaluation_periods=1,
        period=60,
        statistic="Minimum",
    )
    
    alarms["ml_asg_cpu_high"] = create_alarm(
        name="ml-asg-cpu-high",
        namespace="AWS/AutoScaling",
        metric="GroupInServiceInstances",
        threshold=80,
        comparison="GreaterThanThreshold",
        alarm_actions=alarm_actions,
        dimensions={"AutoScalingGroupName": ml_asg_name},
        evaluation_periods=3,
        period=300,
        statistic="Average",
    )
    
    # RDS alarms
    alarms["rds_cpu_high"] = create_alarm(
        name="rds-cpu-high",
        namespace="AWS/RDS",
        metric="CPUUtilization",
        threshold=80.0,
        comparison="GreaterThanThreshold",
        alarm_actions=alarm_actions,
        dimensions={"DBInstanceIdentifier": rds_instance_id},
        evaluation_periods=3,
        period=300,
        statistic="Average",
    )
    
    alarms["rds_storage_low"] = create_alarm(
        name="rds-storage-low",
        namespace="AWS/RDS",
        metric="FreeStorageSpace",
        threshold=2 * 1024 * 1024 * 1024,  # 2 GB
        comparison="LessThanThreshold",
        alarm_actions=alarm_actions,
        dimensions={"DBInstanceIdentifier": rds_instance_id},
        evaluation_periods=1,
        period=300,
        statistic="Average",
    )
    
    # Redis alarms
    alarms["redis_cpu_high"] = create_alarm(
        name="redis-cpu-high",
        namespace="AWS/ElastiCache",
        metric="CPUUtilization",
        threshold=80.0,
        comparison="GreaterThanThreshold",
        alarm_actions=alarm_actions,
        dimensions={"ReplicationGroupId": redis_replication_group_id},
        evaluation_periods=3,
        period=300,
        statistic="Average",
    )
    
    alarms["redis_memory_high"] = create_alarm(
        name="redis-memory-high",
        namespace="AWS/ElastiCache",
        metric="DatabaseMemoryUsagePercentage",
        threshold=85.0,
        comparison="GreaterThanThreshold",
        alarm_actions=alarm_actions,
        dimensions={"ReplicationGroupId": redis_replication_group_id},
        evaluation_periods=3,
        period=300,
        statistic="Average",
    )
    
    # MLOps alarms
    alarms["scaler_forecast_high"] = create_alarm(
        name="scaler-forecast-high",
        namespace="MLOps/Scaler",
        metric="ForecastedLoad",
        threshold=1000,
        comparison="GreaterThanThreshold",
        alarm_actions=alarm_actions,
        dimensions={"ASG": ml_asg_name},
        evaluation_periods=1,
        period=300,
        statistic="Maximum",
    )
    
    alarms["drift_feature_psi"] = create_alarm(
        name="drift-feature-psi",
        namespace="MLOps/DriftMonitor",
        metric="FeatureDriftPSI",
        threshold=0.2,
        comparison="GreaterThanThreshold",
        alarm_actions=alarm_actions,
        dimensions={"Model": "sentiment"},
        evaluation_periods=3,
        period=3600,  # 1 hour
        statistic="Maximum",
    )
    
    return alarms


def provision_monitoring(alb_arn: str, ml_asg_name: str, rds_instance_id: str,
                        redis_replication_group_id: str, sns_topic_arn: str = None) -> dict:
    """
    Orchestrator for monitoring provisioning.
    
    Args:
        alb_arn: ALB ARN
        ml_asg_name: ML ASG name
        rds_instance_id: RDS instance ID
        redis_replication_group_id: Redis replication group ID
        sns_topic_arn: Optional SNS topic for notifications
        
    Returns:
        dict with dashboard_arn and alarm ARNs
    """
    log.info("=== Phase 6: Monitoring & Observability ===")
    
    # Create MLOps dashboard
    dashboard_arn = create_mlops_dashboard()
    
    # Create standard alarms
    alarms = create_standard_alarms(
        alb_arn=alb_arn,
        ml_asg_name=ml_asg_name,
        rds_instance_id=rds_instance_id,
        redis_replication_group_id=redis_replication_group_id,
        sns_topic_arn=sns_topic_arn,
    )
    
    return {
        "dashboard_arn": dashboard_arn,
        **alarms,
    }