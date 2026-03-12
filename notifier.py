"""AWS SNS notifier for bus delay alerts."""

import logging
import os

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)


class SNSNotifier:
    """Sends SMS alerts via AWS SNS.

    AWS credentials are resolved automatically via the boto3 credential chain:
    IAM role > ~/.aws/credentials > environment variables. No keys are read here.
    """

    def __init__(self):
        region = os.environ.get("AWS_REGION", "ca-central-1")
        self.client = boto3.client("sns", region_name=region)

    def send_sms(self, phone_number: str, message: str) -> bool:
        """
        Send an SMS to phone_number (E.164 format, e.g. +16131234567).
        Returns True on success, False on failure.
        """
        try:
            response = self.client.publish(
                PhoneNumber=phone_number,
                Message=message,
                MessageAttributes={
                    "AWS.SNS.SMS.SMSType": {
                        "DataType": "String",
                        "StringValue": "Transactional",
                    },
                    "AWS.SNS.SMS.SenderID": {
                        "DataType": "String",
                        "StringValue": "BusAlert",
                    },
                },
            )
            logger.info("SMS sent. MessageId: %s", response["MessageId"])
            return True
        except (BotoCoreError, ClientError) as e:
            logger.error("Failed to send SMS: %s", e)
            return False

    def send_delay_alert(self, phone_number: str, route_name: str,
                         expected_minutes: int, actual_minutes: int,
                         delay_minutes: int) -> bool:
        message = (
            f"Bus Delay Alert: {route_name}\n"
            f"Expected: {expected_minutes} min | Current: {actual_minutes} min\n"
            f"Delay: ~{delay_minutes} min. Plan accordingly!"
        )
        return self.send_sms(phone_number, message)

    def send_no_service_alert(self, phone_number: str, route_name: str,
                              reason: str = "") -> bool:
        message = (
            f"Bus Alert: {route_name}\n"
            f"No transit directions found.{' ' + reason if reason else ''}\n"
            f"Check alternatives before leaving."
        )
        return self.send_sms(phone_number, message)
