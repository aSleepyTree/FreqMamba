import logging
logger = logging.getLogger('base')


def create_model(opt):
    model = opt['model']
    if model == 'video_base3':
        from .Video_base_model4 import VideoBaseModel as M
    elif model == 'video_base4':
        from .Video_base_model4 import VideoBaseModel as M
    elif model == 'video_base4_m':
        from .Video_base_model4 import VideoBaseModel as M
    elif model == 'retinex':
        from .Video_base_model4 import VideoBaseModel as M
    elif model == 'mamba_restoration':
        from .image_restoration_model import ImageCleanModel as M
    elif model == 'mamba_restoration_test':
        from .image_restoration_model_test import ImageCleanModel as M
    else:
        raise NotImplementedError('Model [{:s}] not recognized.'.format(model))
    m = M(opt)
    logger.info('Model [{:s}] is created.'.format(m.__class__.__name__))
    return m
