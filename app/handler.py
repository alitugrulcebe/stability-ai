import boto3
import json
import logging
import os
import requests
from botocore.exceptions import ClientError
from datetime import datetime, timedelta

QUEUE_URL = os.getenv('QUEUE_URL') 
QUEUE_WEBHOOK_URL = os.getenv('QUEUE_WEBHOOK_URL') 
REPLICATE_URL = os.environ['REPLICATE_URL']
REPLICATE_API_TOKEN = os.environ['REPLICATE_API_TOKEN']
REPLICATE_MODEL_ID = os.environ['REPLICATE_MODEL_ID']
DYNAMODB_TABLE = os.environ['DYNAMODB_TABLE']

sqs = boto3.client('sqs')
ddb = boto3.resource('dynamodb')
table = ddb.Table(DYNAMODB_TABLE)

log = logging.getLogger("Run-Lambda")
default_log_args = {
        "level": logging.DEBUG if os.environ.get("DEBUG", False) else logging.INFO,
        "format": "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        "datefmt": "%d-%b-%y %H:%M",
        "force": True,
    }

logging.basicConfig(**default_log_args)
logger = logging.getLogger(__name__)

def consumer(event, context):
    
    for record in event['Records']:
        logger.info("Message has been published by Tugrul")
        logger.info(f'Record: {record}')
        ai_request_dict = json.loads(record['body'])
        print(ai_request_dict)
        ai_data = ai_request_dict['ai_request']
        process_replicate_webhook(ai_data)

def process_replicate_webhook(ai_data):

    prompt = ai_data['prompt']
    replicate_webhook_url = ai_data['replicate_webhook_url']
    client_id = ai_data['client_id']
    uid = ai_data['uid']    

    logger.info(f'Webhook URL: {replicate_webhook_url}')
    logger.info(f'Prompt: {prompt}')
    logger.info(f'ClientId: {client_id}')
    logger.info(f'UniqueId: {uid}')
    logger.info(f'Replicate model id: {REPLICATE_MODEL_ID}')

    custom_url = replicate_webhook_url + "?client_id=" + client_id + "&uid=" + uid
    prediction = requests.post(REPLICATE_URL, json = {
            "version" : REPLICATE_MODEL_ID,
            "input": {
                "prompt": prompt
            },
            "webhook": custom_url,
            "webhook_events_filter": ["completed"]
        }, headers={'Authorization': 'Token ' + REPLICATE_API_TOKEN})

    logger.info(f'Prediction: {prediction}')

    if prediction.status_code != 201:                 
        send_replicate_message(ai_data)


def webhook_consumer(event, context): 
    for record in event['Records']:
        logger.info("Message has been published by Tugrul")
        logger.info(f'Record: {record}')
        user_request_dict = json.loads(record['body'])
        user_data = user_request_dict['user_data']
        retry = 3
        webhook_retry_count = user_data['webhook_retry_count']   
        if webhook_retry_count <= 0:
            update_failed_image(user_data['pk'], user_data['uid'])
            return user_data


        webhook_url = user_data['webhook_url']
        is_processed = False
        while retry > 0 and webhook_retry_count > 0:
            webhook = requests.post(webhook_url, json = {                
                "id": user_data['uid'],
                "url": user_data['image_url'],
                "status": user_data['image_status'],
                "created_at": user_data['created_at']
            })

            if webhook.status_code == 200:                 
                is_processed = True
                break
            else:
                retry = retry - 1 

        if is_processed is not True:
            user_data['webhook_retry_count'] = webhook_retry_count - 1
            send_webhook_message(user_data)

def send_replicate_message(ai_json):
    try:
        sqs.send_message(
            QueueUrl=QUEUE_URL,
            MessageBody= json.dumps({ "ai_request" : ai_json })
        )
        print(ai_json)
    except ClientError as err:
        logger.error(
                "Couldn't send item %s to queue %s. Here's why: %s: %s", ai_json, QUEUE_URL,
                err.response['Error']['Code'], err.response['Error']['Message'])

def send_webhook_message(user_json):
    try:
        sqs.send_message(
            QueueUrl=QUEUE_WEBHOOK_URL,
            MessageBody= json.dumps({ "user_data" : user_json })
        )
        print(user_json)
    except ClientError as err:
        logger.error(
                "Couldn't send item %s to queue %s. Here's why: %s: %s", user_json, QUEUE_URL,
                err.response['Error']['Code'], err.response['Error']['Message'])

def update_failed_image(clien_id, uid):
    update_query = 'set image_status = :image_status, updated_at = :updated_at'
    table.update_item(
                Key={
                    'pk': clien_id,
                    'sk': REPLICATE_MODEL_ID + '#' + uid,
                },
                UpdateExpression=update_query,
                ExpressionAttributeValues={
                    ':image_status': 'FAILED', 
                    ':updated_at': str(datetime.now().strftime("%m-%d-%Y %H:%M:%S"))
                },
                ReturnValues="UPDATED_NEW"
            )