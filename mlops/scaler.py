"""
Phase 6 — Predictive Auto-Scaling

A Python Lambda reads CloudWatch history, runs a Prophet forecast,
and schedules ASG actions 30 min ahead of predicted spikes.

Every function is idempotent and designed for Lambda execution.
"""

import logging
import os
from datetime import datetime, timedelta
from typing import Any, Optional

import boto3
import pandas as pd

from infra import config
from utils.naming import resource_name

log = logging.getLogger(__name__)

# Try to import Prophet, fallback gracefully
try:
    from prophet import Prophet
    PROPHET_AVAILABLE = True
except ImportError:
    PROPHET_AVAILABLE = False
    log.warning("Prophet not installed, using fallback forecasting")


def get_cloudwatch_client():
    """Get CloudWatch client using shared infra pattern."""
    endpoint = os.getenv("LOCALSTACK_ENDPOINT")
    region = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
    return boto3.client(
        "cloudwatch",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "test"),
    )


def get_autoscaling_client():
    """Get AutoScaling client using shared infra pattern."""
    endpoint = os.getenv("LOCALSTACK_ENDPOINT")
    region = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
    return boto3.client(
        "autoscaling",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "test"),
    )


def fetch_request_history(asg_name: str, days: int = 7) -> pd.DataFrame:
    """
    Reads CloudWatch RequestCount metric history for the ALB.
    Returns tidy DataFrame with ds (timestamp) and y (request count) columns.
    
    Args:
        asg_name: AutoScaling Group name (used to find associated ALB/TG)
        days: number of days of history to fetch
        
    Returns:
        DataFrame with columns: ds (datetime), y (request count)
    """
    cw = get_cloudwatch_client()
    
    # For ALB RequestCount, we need the ALB name, not ASG name
    # In practice, we'd look up the ALB associated with this ASG
    # For now, use a standard naming pattern
    alb_name = resource_name("alb")
    
    end_time = datetime.utcnow()
    start_time = end_time - timedelta(days=days)
    
    log.info(f"Fetching RequestCount for ALB {alb_name} from {start_time} to {end_time}")
    
    try:
        response = cw.get_metric_statistics(
            Namespace="AWS/ApplicationELB",
            MetricName="RequestCount",
            Dimensions=[
                {"Name": "LoadBalancer", "Value": alb_name},
            ],
            StartTime=start_time,
            EndTime=end_time,
            Period=300,  # 5-minute periods
            Statistics=["Sum"],
        )
        
        datapoints = response["Datapoints"]
        if not datapoints:
            log.warning("No datapoints returned from CloudWatch")
            return _empty_dataframe()
        
        # Convert to DataFrame
        df = pd.DataFrame(datapoints)
        df = df.sort_values("Timestamp")
        
        # Prophet expects 'ds' (datetime) and 'y' (value) columns
        df = df.rename(columns={"Timestamp": "ds", "Sum": "y"})
        df["ds"] = pd.to_datetime(df["ds"]).dt.tz_localize(None)
        
        # Forward-fill any gaps
        df = _forward_fill_gaps(df)
        
        log.info(f"Fetched {len(df)} datapoints for request history")
        return df
        
    except Exception as e:
        log.error(f"Failed to fetch request history: {e}")
        return _empty_dataframe()


def _empty_dataframe() -> pd.DataFrame:
    """Returns empty DataFrame with Prophet-required columns."""
    return pd.DataFrame(columns=["ds", "y"])


def _forward_fill_gaps(df: pd.DataFrame) -> pd.DataFrame:
    """Forward-fill gaps in time series data."""
    if df.empty:
        return df
    
    # Create complete 5-minute interval range
    full_range = pd.date_range(
        start=df["ds"].min(),
        end=df["ds"].max(),
        freq="5min",
    )
    
    # Reindex and forward fill
    df = df.set_index("ds").reindex(full_range).ffill().reset_index()
    df = df.rename(columns={"index": "ds"})
    
    return df


def forecast_load(history: pd.DataFrame, horizon_minutes: int = 30) -> pd.DataFrame:
    """
    Fits Facebook Prophet on history and forecasts future load.
    
    Args:
        history: DataFrame with ds and y columns
        horizon_minutes: forecast horizon in minutes
        
    Returns:
        DataFrame with ds, yhat, yhat_lower, yhat_upper, and uncertainty flag
    """
    if not PROPHET_AVAILABLE:
        return _fallback_forecast(history, horizon_minutes)
    
    if history.empty or len(history) < 10:
        log.warning("Insufficient history for Prophet, using fallback")
        return _fallback_forecast(history, horizon_minutes)
    
    try:
        # Prepare data for Prophet
        df = history[["ds", "y"]].copy()
        df["y"] = df["y"].astype(float)
        
        # Initialize and fit Prophet model
        model = Prophet(
            daily_seasonality=True,
            weekly_seasonality=True,
            yearly_seasonality=False,
            changepoint_prior_scale=0.05,
            seasonality_prior_scale=10.0,
            interval_width=0.8,  # 80% confidence interval
        )
        
        model.fit(df)
        
        # Make future dataframe
        future = model.make_future_dataframe(
            periods=horizon_minutes // 5,  # 5-minute intervals
            freq="5min",
        )
        
        # Forecast
        forecast = model.predict(future)
        
        # Select relevant columns
        result = forecast[["ds", "yhat", "yhat_lower", "yhat_upper"]].copy()
        result = result.tail(horizon_minutes // 5)
        
        # Flag high-uncertainty forecasts
        result["uncertainty_ratio"] = result["yhat_upper"] / result["yhat"].replace(0, 1)
        result["high_uncertainty"] = result["uncertainty_ratio"] > 2.0
        
        # Ensure non-negative predictions
        result["yhat"] = result["yhat"].clip(lower=0)
        result["yhat_lower"] = result["yhat_lower"].clip(lower=0)
        result["yhat_upper"] = result["yhat_upper"].clip(lower=0)
        
        log.info(f"Prophet forecast completed: {len(result)} periods")
        return result
        
    except Exception as e:
        log.error(f"Prophet forecast failed: {e}")
        return _fallback_forecast(history, horizon_minutes)


def _fallback_forecast(history: pd.DataFrame, horizon_minutes: int) -> pd.DataFrame:
    """Simple fallback forecast using moving average."""
    if history.empty:
        return pd.DataFrame(columns=["ds", "yhat", "yhat_lower", "yhat_upper", "high_uncertainty"])
    
    # Use recent average
    recent_avg = history["y"].tail(12).mean()  # Last hour
    last_time = history["ds"].max()
    
    periods = horizon_minutes // 5
    future_times = pd.date_range(
        start=last_time + pd.Timedelta(minutes=5),
        periods=periods,
        freq="5min",
    )
    
    result = pd.DataFrame({
        "ds": future_times,
        "yhat": recent_avg,
        "yhat_lower": recent_avg * 0.5,
        "yhat_upper": recent_avg * 1.5,
        "high_uncertainty": False,
    })
    
    log.info(f"Fallback forecast: {recent_avg:.2f} avg requests/5min")
    return result


def schedule_scaling_action(asg_name: str, desired: int,
                           at: datetime, expiry_minutes: int = 60) -> str:
    """
    Calls autoscaling:PutScheduledUpdateGroupAction.
    Action expires after expiry_minutes so a bad forecast self-heals.
    
    Args:
        asg_name: AutoScaling Group name
        desired: desired capacity to schedule
        at: datetime to execute the scaling action
        expiry_minutes: minutes after which the action expires
        
    Returns:
        scheduled_action_name
    """
    asg_client = get_autoscaling_client()
    
    action_name = f"{asg_name}-scale-{at.strftime('%Y%m%d-%H%M%S')}"
    end_time = at + timedelta(minutes=expiry_minutes)
    
    # Cap desired capacity at ASG max size
    try:
        asg_info = asg_client.describe_auto_scaling_groups(
            AutoScalingGroupNames=[asg_name]
        )
        if asg_info["AutoScalingGroups"]:
            max_size = asg_info["AutoScalingGroups"][0]["MaxSize"]
            desired = min(desired, max_size)
    except Exception as e:
        log.warning(f"Could not fetch ASG max size: {e}")
    
    log.info(f"Scheduling scaling action for {asg_name}: desired={desired} at {at} (expires {end_time})")
    
    try:
        asg_client.put_scheduled_update_group_action(
            AutoScalingGroupName=asg_name,
            ScheduledActionName=action_name,
            DesiredCapacity=desired,
            StartTime=at,
            EndTime=end_time,
        )
        log.info(f"Scheduled action created: {action_name}")
        return action_name
    except Exception as e:
        log.error(f"Failed to schedule scaling action: {e}")
        raise


def compute_desired_capacity(current: int, forecast: pd.DataFrame,
                           target_utilization: float = 0.7,
                           max_instances: int = 4,
                           min_instances: int = 1) -> int:
    """
    Computes desired capacity based on forecasted load.
    
    Args:
        current: current number of instances
        forecast: forecast DataFrame with yhat column
        target_utilization: target CPU/utilization per instance
        max_instances: maximum instances
        min_instances: minimum instances
        
    Returns:
        desired capacity
    """
    if forecast.empty:
        return current
    
    # Use the peak forecasted load in the horizon
    peak_load = forecast["yhat"].max()
    
    # Estimate requests per instance per 5 minutes (tunable)
    requests_per_instance = 100
    
    desired = int(peak_load / (requests_per_instance * target_utilization)) + 1
    desired = max(min_instances, min(desired, max_instances))
    
    log.info(f"Computed desired capacity: {desired} (peak load: {peak_load:.0f}, current: {current})")
    return desired


def run(event: dict, context: Any) -> dict:
    """
    Lambda handler — orchestrates fetch → forecast → schedule.
    
    Args:
        event: Lambda event (can contain ASG name override)
        context: Lambda context
        
    Returns:
        dict with status and details
    """
    log.info("Starting predictive scaling run")
    
    # Get ASG name from event or environment
    asg_name = event.get("asg_name", resource_name("ml-asg"))
    horizon_minutes = event.get("horizon_minutes", 30)
    history_days = event.get("history_days", 7)
    
    # Fetch request history
    history = fetch_request_history(asg_name, days=history_days)
    
    # Forecast load
    forecast = forecast_load(history, horizon_minutes=horizon_minutes)
    
    # Check for high uncertainty
    if forecast.empty:
        return {
            "status": "skipped",
            "reason": "No forecast data available",
        }
    
    if forecast["high_uncertainty"].any():
        log.warning("High uncertainty in forecast, skipping scaling action")
        return {
            "status": "skipped",
            "reason": "High uncertainty in forecast",
            "uncertainty_ratio": forecast["uncertainty_ratio"].max(),
        }
    
    # Get current ASG desired capacity
    asg_client = get_autoscaling_client()
    try:
        asg_info = asg_client.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
        current_desired = asg_info["AutoScalingGroups"][0]["DesiredCapacity"]
    except Exception as e:
        log.error(f"Failed to get current ASG capacity: {e}")
        return {"status": "error", "reason": str(e)}
    
    # Compute desired capacity
    desired = compute_desired_capacity(current_desired, forecast)
    
    # Only schedule if different from current
    if desired == current_desired:
        log.info(f"Desired capacity unchanged ({desired}), no action needed")
        return {
            "status": "no_change",
            "current": current_desired,
            "desired": desired,
        }
    
    # Schedule scaling action
    at = datetime.utcnow() + timedelta(minutes=5)  # Start in 5 minutes
    action_name = schedule_scaling_action(asg_name, desired, at)
    
    # Publish metric for monitoring
    try:
        from infra import monitoring
        monitoring.put_metric(
            namespace="MLOps/Scaler",
            metric_name="ForecastedLoad",
            value=forecast["yhat"].max(),
            dimensions={"ASG": asg_name},
        )
        monitoring.put_metric(
            namespace="MLOps/Scaler",
            metric_name="ScalingActionScheduled",
            value=1,
            dimensions={"ASG": asg_name},
        )
    except Exception as e:
        log.warning(f"Failed to publish scaler metrics: {e}")
    
    return {
        "status": "scheduled",
        "action_name": action_name,
        "current_capacity": current_desired,
        "new_capacity": desired,
        "forecast_peak": float(forecast["yhat"].max()),
        "scheduled_at": at.isoformat(),
    }