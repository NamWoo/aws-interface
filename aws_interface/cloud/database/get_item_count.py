from cloud.aws import *

# Define the input output format of the function.
# This information is used when creating the *SDK*.
info = {
    'input_format': {
        'partition': 'str',
    },
    'output_format': {
        'item': {
            'count': 'int'
        }
    }
}


def do(data, boto3):
    response = {}
    recipe = data['recipe']
    app_id = data['app_id']
    params = data['params']

    table_name = '{}-{}'.format(recipe['recipe_type'], app_id)
    partition = params['partition']

    dynamo = DynamoDB(boto3)
    count = dynamo.get_item_count(table_name, '{}-count'.format(partition))
    item = count.get('Item', {'count': 0})
    response['item'] = item
    return response