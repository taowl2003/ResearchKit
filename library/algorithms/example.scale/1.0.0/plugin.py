"""The single teaching algorithm: multiply a scalar by a factor."""


def run(inputs, parameters, context):
    return {'value': inputs['value'] * parameters['factor']}
