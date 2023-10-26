import os
import requests
import json
import boto3
import logging
import math
import random
import uuid
import replicate
from app.utils import is_valid_url, num_tokens_from_messages
from better_profanity import profanity
from botocore.exceptions import ClientError
from datetime import datetime, timedelta
from fastapi import FastAPI, APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from io import BytesIO
from mangum import Mangum
from PIL import Image
from starlette.requests import Request
from starlette.responses import Response

stage = os.environ.get('STAGE', None)
openapi_prefix = f"/{stage}" if stage else "/"

app = FastAPI(title="Stability AI API", root_path=openapi_prefix) # Here is the magic

QUEUE_URL = os.getenv('QUEUE_URL') 
QUEUE_WEBHOOK_URL = os.getenv('QUEUE_WEBHOOK_URL')
REPLICATE_URL = os.environ['REPLICATE_URL']
REPLICATE_API_TOKEN = os.environ['REPLICATE_API_TOKEN']
REPLICATE_MODEL_ID = os.environ['REPLICATE_MODEL_ID']
REPLICATE_SYNC_MODEL_ID = os.environ['REPLICATE_SYNC_MODEL_ID']
S3_BUCKET = os.environ['S3_BUCKET']
AI_MODEL_NAME = os.environ['AI_MODEL_NAME']
DIRECTORY_NAME = os.environ['DIRECTORY_NAME']
DYNAMODB_TABLE = os.environ['DYNAMODB_TABLE']

sqs = boto3.client('sqs')
s3 = boto3.resource('s3')
ddb = boto3.resource('dynamodb')
# ddb = boto3.resource('dynamodb', endpoint_url='http://localhost:8000')
table = ddb.Table(DYNAMODB_TABLE)

api_router = APIRouter()

profanity.load_censor_words()

async def catch_exceptions_middleware(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as err:
        logger.error(f'Error is : {err}')        
        return Response("Internal server error", status_code=200)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.middleware('http')(catch_exceptions_middleware)

log = logging.getLogger("Run-Lambda")

default_log_args = {
        "level": logging.DEBUG if os.environ.get("DEBUG", False) else logging.INFO,
        "format": "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        "datefmt": "%d-%b-%y %H:%M",
        "force": True,
    }

logging.basicConfig(**default_log_args)
logger = logging.getLogger(__name__)

@api_router.post("/images")
async def generate_images(request: Request):
    request_body = await request.json()
    logger.info(f'request object is : {request_body}')
    logger.info(f'request headers are : {request.headers}')

    client_id = request.headers.get('X-RapidAPI-Key')

    if client_id is None:
        return Response('Rapid API key is required field', status_code=200)        

    if 'prompt' not in request_body:
        return Response('Prompt is required field', status_code=200)    
    
    if num_tokens_from_messages(request_body['prompt']) > 250:
        return Response('Prompt is too long', status_code=200)

    if profanity.contains_profanity(request_body['prompt']) is True:
        return Response('Prompt contains profanity', status_code=200)
    
    process = request.headers.get('X-RapidAPI-Processor')
    webhook_base_url = 'https://obc75b1o88.execute-api.us-east-1.amazonaws.com/v1/'

    if process == 'sync':
        return await generate_image_sync(client_id, request_body)
    elif process == 'async':
        return await generate_images_async(client_id, request_body, webhook_base_url) #request.base_url._url)
    else:
        return Response('Processor is not valid', status_code=200)
    
        

async def generate_image_sync(client_id, request_body):
    if client_id == 'Application-RAPID_KEY':
        return {
                "id": uuid.uuid4().hex,
                "url": "https://general-ai-images-public.s3.amazonaws.com/alice-wonderland.png",
                "status": "COMPLETED"  
            }
    
    now = datetime.now()

    output = []
    try:
        output = list(replicate.run(REPLICATE_SYNC_MODEL_ID, input={"prompt": request_body['prompt']}))
    except Exception as err:
        logger.error(f'Error is : {err}')
        return Response('AI Model is timedout, please try asyncronous call', status_code=200)
    
    if len(output) > 0:
        logger.info(f'output: {output[0]}')
        # Fetch image from URL
        response = requests.get(output[0])
        
        image = Image.open(BytesIO(response.content))
        # # Save the image to an in-memory file
        in_mem_file = BytesIO()
        image.save(in_mem_file, format=image.format)
        in_mem_file.seek(0)

        bucket = s3.Bucket(S3_BUCKET)
        expires = now + timedelta(minutes=5)
        expires = expires.isoformat()

        image_client_path = client_id
        if client_id == 'test_user':
            image_client_path += '-' + str(math.floor(random.random() * 100000000000000000))
        
        image_name = 'ai-' + image_client_path + '-' + str(now.strftime("%m-%d-%Y")) + '.png'
        
        bucket.put_object(Key=("{}/{}/{}").format(AI_MODEL_NAME, DIRECTORY_NAME, image_name), Body=in_mem_file, Expires=expires, ContentType="image", ContentDisposition="inline")
        
        s3_url = 'https://' + S3_BUCKET + '.s3.amazonaws.com/' + AI_MODEL_NAME + '/' + DIRECTORY_NAME + '/' + image_name
        uid = uuid.uuid4().hex

        data = table.put_item(
                Item={
                    'pk': client_id,
                    'sk': REPLICATE_MODEL_ID + '#' + uid,
                    'prompt':request_body['prompt'],
                    'image_url' : s3_url,    
                    'image_status': 'COMPLETED',
                    'webhook_url': '',
                    'created_at': str(now.strftime("%m-%d-%Y %H:%M:%S")),
                    'updated_at': str(now.strftime("%m-%d-%Y %H:%M:%S"))            
                }
            )
        if data:         
            return { 
                    "id": uid,
                    "url": s3_url,
                    "status": "COMPLETED"                
                }
        else:
            return {
                "message": "Creating ai image failed"
            }

async def generate_images_async(client_id, request_body, base_url):
    if client_id == 'Application-RAPID_KEY':
        return {
                "id": uuid.uuid4().hex,
                "status": "PENDING"
            }

    if 'webhook_url' in request_body and is_valid_url(request_body['webhook_url']) is False:
        return Response('Webhook URL is not valid', status_code=200)

    now = datetime.now()    
    REPLICATE_WEBHOOK_URL = base_url + "webhook"
    ai_json = dict()  
    ai_json['replicate_webhook_url'] = REPLICATE_WEBHOOK_URL
    ai_json['client_id'] = client_id
    ai_json['uid'] = uuid.uuid4().hex
    ai_json['prompt'] = request_body['prompt']
    ai_json['webhook_url'] = request_body['webhook_url'] if 'webhook_url' in request_body else ''

    try:
        data = table.put_item(
            Item={
                'pk': client_id,
                'sk': REPLICATE_MODEL_ID + '#' + ai_json['uid'],
                'prompt': ai_json['prompt'],
                'image_url' : '',    
                'image_status': 'PENDING',
                'webhook_url': ai_json['webhook_url'],
                'created_at': str(now.strftime("%m-%d-%Y %H:%M:%S")),
                'updated_at': str(now.strftime("%m-%d-%Y %H:%M:%S"))            
            }
        )
        logger.info(f'Data is inserted into DynamoDB')
        logger.info(f'data: {data}')
        if data: 
            send_message(ai_json)

        return JSONResponse(content={ "id": ai_json['uid'], "status": "PENDING" }, status_code=202)
    
    except ClientError as err:
        logger.error(
                "Couldn't update item %s in table %s. Here's why: %s: %s", ai_json['uid'], DYNAMODB_TABLE,
                err.response['Error']['Code'], err.response['Error']['Message'])
        return {
            "message": "Creating ai image failed"
        }
    
@api_router.get("/images/{uid}")
def get_image(uid: str, request: Request):
    client_id = request.headers.get('X-RapidAPI-Key')   
    if client_id == 'Application-RAPID_KEY':
        return {
            "id": uid,            
            "url": 'image_url',
            "status": 'COMPLETED',
            "created_at": datetime.now()
        }

    try:
        item_fetched = table.get_item(
            TableName=DYNAMODB_TABLE,
            Key={
                    'pk': client_id,
                    'sk': REPLICATE_MODEL_ID + '#' + uid
                }
        )
        logger.info(f'Fetched item: {item_fetched}')
        data_json = json.loads(json.dumps(item_fetched))
        if 'Item' in data_json:
            item = data_json['Item']

            if item != None and item['image_status'] == 'COMPLETED':
                return {
                    "id": uid,
                    "url": item['image_url'],
                    "status": item['image_status'],                
                    "created_at": item['created_at']
                }
            else:
                return {
                    "id": uid,
                    "status": "PENDING"
                }
        else:
            return {
                "message": "Id is not valid"
            }
    except ClientError as err:
        logger.error(
                "Couldn't get item %s in table %s. Here's why: %s: %s", uid, DYNAMODB_TABLE,
                err.response['Error']['Code'], err.response['Error']['Message'])    
        return {
            "message": "Fetching image failed"
        }
    
@api_router.post("/webhook", status_code=200, include_in_schema=False)
async def webhook(client_id: str, uid: str, request: Request):
    webhook_dict = await request.json()  
    logger.info(f'Webhook data : {webhook_dict}')
    url = webhook_dict['output'][0]
    
    # # Find the bucket name
    now = datetime.now()
    # month = '{:02d}'.format(now.month)

    # # Fetch image from URL
    response = requests.get(url)
    image = Image.open(BytesIO(response.content))
    # # Save the image to an in-memory file
    in_mem_file = BytesIO()
    image.save(in_mem_file, format=image.format)
    in_mem_file.seek(0)

    bucket = s3.Bucket(S3_BUCKET)
    expires = now + timedelta(days=7)
    expires = expires.isoformat()
    s3_image = bucket.put_object(Key=("{}/{}/{}").format(AI_MODEL_NAME, DIRECTORY_NAME, 'ai-' + str(now.strftime("%m-%d-%Y")) + '.png'), Body=in_mem_file, Expires=expires, ContentType="image", ContentDisposition="inline")

    logger.info(f'Image S3: {s3_image}')
    logger.info(f'clientId: {client_id}')

    image_client_path = client_id
    if client_id == 'test_user':
        image_client_path += '-' + str(math.floor(random.random() * 100000000000000000))
    
    s3_url = 'https://' + S3_BUCKET + '.s3.amazonaws.com/' + AI_MODEL_NAME + '/' + DIRECTORY_NAME + '/' + 'ai-' + image_client_path + '-' + str(now.strftime("%m-%d-%Y")) + '.png'
    logger.info(f's3_url: {s3_url}')

    logger.info(f'Data update started into DynamoDB')

    try:        
        update_query = 'set image_status = :image_status, image_url = :image_url, updated_at = :updated_at'
        data = table.update_item(
            Key={
                'pk': client_id,
                'sk': REPLICATE_MODEL_ID + '#' + uid,
            },
            UpdateExpression=update_query,
            ExpressionAttributeValues={
                ':image_status': 'COMPLETED', 
                ':image_url': s3_url, 
                ':updated_at': str(now.strftime("%m-%d-%Y %H:%M:%S"))
            },
            ReturnValues="UPDATED_NEW"
        )
        logger.info(f'Data is updated into DynamoDB')
        logger.info(f'data: {data}')
        
        item_fetched = table.get_item(
            TableName=DYNAMODB_TABLE,
            Key={
                    'pk': client_id,
                    'sk': REPLICATE_MODEL_ID + '#' + uid
                }
        )
        data_json = json.loads(json.dumps(item_fetched))
        if 'Item' in data_json:
            user_data = data_json['Item']
            webhook_url = user_data['webhook_url']

            if len(webhook_url) > 0:
                user_data['webhook_retry_count'] = 3
                user_data['uid'] = uid
                user_data['image_status'] = 'COMPLETED'
                send_webhook_message(user_data)
 
        return webhook_dict
    except ClientError as err:
        logger.error(
                "Couldn't update item %s in table %s. Here's why: %s: %s", uid, DYNAMODB_TABLE,
                err.response['Error']['Code'], err.response['Error']['Message'])
        update_query = 'set image_status = :image_status, updated_at = :updated_at'
        data = table.update_item(
            Key={
                'pk': client_id,
                'sk': REPLICATE_MODEL_ID + '#' + uid,
            },
            UpdateExpression=update_query,
            ExpressionAttributeValues={
                ':image_status': 'FAILED', 
                ':updated_at': str(now.strftime("%m-%d-%Y %H:%M:%S"))
            },
            ReturnValues="UPDATED_NEW"
        )
        return {
            "message": "Updating ai image failed"
        }


def send_message(ai_json):
    try:
        sqs.send_message(
            QueueUrl=QUEUE_URL,
            MessageBody= json.dumps({ "ai_request" : ai_json })
        )
        print(ai_json)
    except ClientError as err:
        logger.error(
                "Couldn't send item %s to queue %s", ai_json, QUEUE_URL,
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
                "Couldn't send item %s to queue %s", user_json, QUEUE_WEBHOOK_URL,
                err.response['Error']['Code'], err.response['Error']['Message'])


@api_router.post("/test/images", status_code=200, include_in_schema=False)
async def publish_world(request: Request):
    return { 
            "id": uuid.uuid4().hex,
            "status": "PENDING" 
        }


@api_router.get("/test/image/{uid}", include_in_schema=False)
def get_image(uid: str, request: Request):
    return {
            "id": uid,
            "url": 'image_url',
            "status": 'COMPLETED',
            "created_at": datetime.now()
        }      

app.include_router(api_router)
# handler = Mangum(app)

def handler(event, context):
    logger.info(f'Event: {event}')
    if 'requestContext' not in event:
        event['requestContext'] = {}  # Adds a dummy field; mangum will process this fine
    # print(event)
    asgi_handler = Mangum(app)
    response = asgi_handler(event, context)

    return response


