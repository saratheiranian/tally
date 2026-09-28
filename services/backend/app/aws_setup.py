"""Create queues and the DynamoDB table if missing:  python -m app.aws_setup

Used for LocalStack and tests. In AWS these resources come from Terraform
(Phase 4). Idempotent, so it's safe to run on every start.
"""

import json

from botocore.exceptions import ClientError

from . import aws
from .config import Settings
from .config import settings as default_settings


def ensure_resources(settings: Settings = default_settings) -> dict[str, str]:
    sqs = aws.client("sqs", settings)
    ddb = aws.client("dynamodb", settings)

    dlq_url = sqs.create_queue(
        QueueName=settings.dlq_name,
        Attributes={"MessageRetentionPeriod": str(14 * 24 * 3600)},  # max: time to investigate
    )["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]

    urls = {}
    for name in settings.queue_names():
        urls[name] = sqs.create_queue(
            QueueName=name,
            Attributes={
                "VisibilityTimeout": str(settings.worker_visibility_timeout),
                "RedrivePolicy": json.dumps(
                    {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": str(settings.max_receive_count)}
                ),
            },
        )["QueueUrl"]

    try:
        ddb.create_table(
            TableName=settings.dynamodb_table,
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",  # spiky ingest; no capacity planning needed
        )
        ddb.get_waiter("table_exists").wait(TableName=settings.dynamodb_table)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceInUseException":
            raise
    return urls


if __name__ == "__main__":
    for name, url in ensure_resources().items():
        print(f"{name}: {url}")
