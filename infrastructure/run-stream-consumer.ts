import {
  Duration, RemovalPolicy, aws_cloudwatch as cloudwatch, aws_dynamodb as dynamodb,
  aws_lambda as lambda, aws_lambda_event_sources as sources, aws_sqs as sqs,
} from 'aws-cdk-lib';
import { Construct } from 'constructs';

export function failureQueue(scope: Construct, id: string): sqs.Queue {
  const queue = new sqs.Queue(scope, id, {
    encryption: sqs.QueueEncryption.SQS_MANAGED, enforceSSL: true,
    retentionPeriod: Duration.days(14), removalPolicy: RemovalPolicy.RETAIN,
  });
  new cloudwatch.Alarm(scope, `${id}Alarm`, {
    metric: queue.metricApproximateNumberOfMessagesVisible({ period: Duration.minutes(1) }),
    threshold: 1, evaluationPeriods: 1,
    comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
    treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    alarmDescription: 'Failed run processing requires inspection and replay from DynamoDB',
  });
  return queue;
}

export function monitorFunction(scope: Construct, id: string, handler: lambda.IFunction): void {
  for (const [name, metric] of Object.entries({
    Errors: handler.metricErrors({ period: Duration.minutes(1) }),
    Throttles: handler.metricThrottles({ period: Duration.minutes(1) }),
  })) {
    new cloudwatch.Alarm(scope, `${id}${name}`, {
      metric, threshold: 1, evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
  }
}

export interface RunStreamConsumerProps {
  readonly table: dynamodb.ITable;
  readonly handler: lambda.IFunction;
  readonly kind: 'run' | 'event';
}

/** The two consumers share the run stream's two-reader-per-shard budget. */
export class RunStreamConsumer extends Construct {
  constructor(scope: Construct, id: string, props: RunStreamConsumerProps) {
    super(scope, id);
    const queue = failureQueue(this, 'Failures');
    props.handler.addEventSource(new sources.DynamoEventSource(props.table, {
      startingPosition: lambda.StartingPosition.TRIM_HORIZON,
      batchSize: props.kind === 'run' ? 1 : 10,
      bisectBatchOnError: true, reportBatchItemFailures: true,
      retryAttempts: 3, maxRecordAge: Duration.hours(1),
      onFailure: new sources.SqsDlq(queue),
      filters: [lambda.FilterCriteria.filter({
        eventName: ['INSERT'],
        dynamodb: { NewImage: {
          kind: { S: [props.kind] },
          ...(props.kind === 'run' ? { status: { S: ['pending'] } } : {}),
        } },
      })],
    }));
    monitorFunction(this, 'Handler', props.handler);
    new cloudwatch.Alarm(this, 'IteratorAge', {
      metric: props.handler.metric('IteratorAge', {
        statistic: 'Maximum', period: Duration.minutes(1),
      }),
      threshold: 60_000, evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      alarmDescription: 'Run stream consumer is more than one minute behind',
    });
    new cloudwatch.Alarm(this, 'DestinationDeliveryFailures', {
      metric: props.handler.metric('DestinationDeliveryFailures', {
        statistic: 'Sum', period: Duration.minutes(1),
      }),
      threshold: 1, evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
  }
}
