"""Hello case: 2 multiplied by 3 gives 6."""


def run(context):
    value = context.parameters['value']
    factor = context.document['plugins']['scale']['parameters']['factor']
    result = context.call('scale', inputs={'value': value})['value']
    context.atomic_write_json(context.result_path('summary.json'),
                              {'input': value, 'factor': factor, 'result': result})
    return {'input': value, 'factor': factor, 'result': result}
