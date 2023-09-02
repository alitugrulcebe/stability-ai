import boto3
import json
import logging
import os
import requests
from botocore.exceptions import ClientError
from datetime import datetime, timedelta

REPLICATE_URL = os.environ['REPLICATE_URL']
REPLICATE_API_TOKEN = os.environ['REPLICATE_API_TOKEN']
REPLICATE_MODEL_ID = os.environ['REPLICATE_MODEL_ID']

sqs = boto3.client('sqs')
ddb = boto3.resource('dynamodb')

QUEUE_URL = os.getenv('QUEUE_URL') 
QUEUE_URL = os.getenv('QUEUE_WEBHOOK_URL') 

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
                "uid": user_data['pk'],
                "status": user_data['image_status'],
                "image_url": user_data['image_url'],
                "created_at": user_data['created_at']
            })

            if webhook.status_code == 200:                 
                result = json.loads(webhook.content)
                print(result)
                is_processed = True
                break
            else:
                retry = retry - 1 

            if is_processed is not True:
                webhook_retry_count = webhook_retry_count - 1
                user_data['webhook_retry_count'] = webhook_retry_count
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
            QueueUrl=QUEUE_URL,
            MessageBody= json.dumps({ "user_data" : user_json })
        )
        print(user_json)
    except ClientError as err:
        logger.error(
                "Couldn't send item %s to queue %s. Here's why: %s: %s", user_json, QUEUE_URL,
                err.response['Error']['Code'], err.response['Error']['Message'])

def update_failed_image(clien_id, uid):
    table = ddb.Table('AIRequests')
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


# def bucket_creater(event, context):


# 17-Aug-23 04:00 [INFO] app/handler - Record: {'messageId': '3fbcf2ba-07a5-4f56-9a16-e0325de55d9a', 'receiptHandle': 'AQEBPPU09qu8hoyZRHMxA4GKz9rLqg5v7KVhJbiA8IWd+FlVx8JX+ig30PrWeY8VCyHZaeCShmqdpBJKJaVayUWXx+WWczFRICK3bCHj2gm4Agq1cchn8PUR1dyQLGGQ+VrBmzDmKtWNDeRLfIu8Cyfo9AyzXw7XNiK+m+ImvIUMt21bkqqIP0vS2Isn/Ur9Qd67OHXRnk93Ded5USWXaQUDlKz7dbea8Ybb8Zv2xM0hUg9wVbx/W5mx2nJ9qg6wLH5g3g+cWdV/GGur8mCOjjtU4VehRgp9xDLFoq6w1paqEyoj3XfKA0QF/o0pU1QVvVeZkZQogdZ/aqMTmD77ZrKrzn27wEX8FcgYYr0Bmg5XjLpjTP39ipF1wCjPaGFpZBvSTUclixG7wXgETD4ZCVA9nG57AYaxQmFx9PahmrssyWQ=', 'body': '04:00:06 AM - Hello World!', 'attributes': {'ApproximateReceiveCount': '1', 'AWSTraceHeader': 'Root=1-64dd9b46-2898b7174fbac1c24e5f8fbe;Parent=5cd4329600ed1e48;Sampled=0;Lineage=7b585266:0', 'SentTimestamp': '1692244806992', 'SenderId': 'AROAYF32EE5BRDUOSQNTH:stability-ai-api-dev-app', 'ApproximateFirstReceiveTimestamp': '1692244807003'}, 'messageAttributes': {}, 'md5OfBody': '372367a38568aba05c8d5c4dc82afcf9', 'eventSource': 'aws:sqs', 'eventSourceARN': 'arn:aws:sqs:us-east-1:562359969603:stability-ai-api-dev-ai-requests', 'awsRegion': 'us-east-1'}
# if __name__ == "__main__":
    # prompt = "goofy owl"

    # REPLICATE_URL = 'https://api.replicate.com/v1/predictions'
    # REPLICATE_API_TOKEN = "r8_dn5MfBHwgVLRSQgK949fOGbdnmT2HSp1ZBse6"
    # prediction = requests.post(REPLICATE_URL, json = {
    #     "version" : "2b017d9b67edd2ee1401238df49d75da53c523f36e363881e057f5dc3ed3c5b2",
    #     "input": {
    #         "prompt": prompt
    #     },
    #     "webhook": "www.google.com",
    #     "webhook_events_filter": ["completed"]
    # }, headers={'Authorization': 'Token ' + REPLICATE_API_TOKEN})

    # result = json.loads(prediction.content)
    # print(result)