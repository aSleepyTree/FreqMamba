import importlib
import torch
from collections import OrderedDict
from copy import deepcopy
from os import path as osp
from tqdm import tqdm
from .base_model_for_mamba_test import BaseModel
from models.utils import get_root_logger, imwrite, tensor2img
import math
import matplotlib.pyplot as plt
from einops import rearrange
loss_module = importlib.import_module('basicsr.models.losses')
metric_module = importlib.import_module('basicsr.metrics')
import importlib
from os import path as osp
import torch.nn as nn
from .utils import scandir
import os
import random
import numpy as np
import cv2
import torch.nn.functional as F


from functools import partial
class AMPLoss(nn.Module):
    def __init__(self):
        super(AMPLoss, self).__init__()
        self.cri = nn.L1Loss()

    def forward(self, x, y):
        x = torch.fft.rfft2(x, norm='backward')
        x_mag =  torch.abs(x)
        y = torch.fft.rfft2(y, norm='backward')
        y_mag = torch.abs(y)

        return self.cri(x_mag,y_mag)


class PhaLoss(nn.Module):
    def __init__(self):
        super(PhaLoss, self).__init__()
        self.cri = nn.L1Loss()

    def forward(self, x, y):
        x = torch.fft.rfft2(x, norm='backward')
        x_mag = torch.angle(x)
        y = torch.fft.rfft2(y, norm='backward')
        y_mag = torch.angle(y)

        return self.cri(x_mag, y_mag)
def dynamic_instantiation(modules, cls_type, opt):
    """Dynamically instantiate class.

    Args:
        modules (list[importlib modules]): List of modules from importlib
            files.
        cls_type (str): Class type.
        opt (dict): Class initialization kwargs.

    Returns:
        class: Instantiated class.
    """
    print(modules,'2222222')
    print(cls_type,'8888')
    for module in modules:
        cls_ = getattr(module, cls_type, None)
        if cls_ is not None:
            break
    if cls_ is None:
        raise ValueError(f'{cls_type} is not found.')
    print(cls_)
    # exit()
    return cls_(**opt)

arch_folder = osp.dirname(osp.abspath(__file__))
arch_folder = os.path.join(arch_folder,'archs')
arch_filenames = [
    osp.splitext(osp.basename(v))[0] for v in scandir(arch_folder)
    if v.endswith('_arch.py')
]
_arch_modules = [
    importlib.import_module(f'basicsr.models.archs.{file_name}')
    for file_name in arch_filenames
]
def define_network(opt):
    network_type = opt.pop('type')
    print(_arch_modules, network_type, arch_filenames, arch_folder)
    # exit()
    net = dynamic_instantiation(_arch_modules, network_type, opt)
    return net


class Mixing_Augment:
    def __init__(self, mixup_beta, use_identity, device):
        self.dist = torch.distributions.beta.Beta(torch.tensor([mixup_beta]), torch.tensor([mixup_beta]))
        self.device = device

        self.use_identity = use_identity

        self.augments = [self.mixup]

    def mixup(self, target, input_):
        lam = self.dist.rsample((1,1)).item()
    
        r_index = torch.randperm(target.size(0)).to(self.device)
    
        target = lam * target + (1-lam) * target[r_index, :]
        input_ = lam * input_ + (1-lam) * input_[r_index, :]
    
        return target, input_

    def __call__(self, target, input_):
        if self.use_identity:
            augment = random.randint(0, len(self.augments))
            if augment < len(self.augments):
                target, input_ = self.augments[augment](target, input_)
        else:
            augment = random.randint(0, len(self.augments)-1)
            target, input_ = self.augments[augment](target, input_)
        return target, input_

class ImageCleanModel(BaseModel):
    """Base Deblur model for single image deblur."""

    def __init__(self, opt):
        super(ImageCleanModel, self).__init__(opt)

        # define network

        self.mixing_flag = self.opt['train']['mixing_augs'].get('mixup', False)
        if self.mixing_flag:
            mixup_beta       = self.opt['train']['mixing_augs'].get('mixup_beta', 1.2)
            use_identity     = self.opt['train']['mixing_augs'].get('use_identity', False)
            self.mixing_augmentation = Mixing_Augment(mixup_beta, use_identity, self.device)

        self.net_g = define_network(deepcopy(opt['network_g']))
        self.net_g = self.model_to_device(self.net_g)
        self.print_network(self.net_g)

        # load pretrained models
        load_path = self.opt['path'].get('pretrain_network_g', None)
        print(load_path)
        # exit()
        if load_path is not None:
            self.load_network(self.net_g, load_path,
                              self.opt['path'].get('strict_load_g', True), param_key=self.opt['path'].get('param_key', 'params'))

        if self.is_train:
            self.init_training_settings()

    def init_training_settings(self):
        self.net_g.train()
        train_opt = self.opt['train']

        self.ema_decay = train_opt.get('ema_decay', 0)
        if self.ema_decay > 0:
            logger = get_root_logger()
            logger.info(
                f'Use Exponential Moving Average with decay: {self.ema_decay}')
            # define network net_g with Exponential Moving Average (EMA)
            # net_g_ema is used only for testing on one GPU and saving
            # There is no need to wrap with DistributedDataParallel
            self.net_g_ema = define_network(self.opt['network_g']).to(
                self.device)
            # load pretrained model
            load_path = self.opt['path'].get('pretrain_network_g', None)
            if load_path is not None:
                self.load_network(self.net_g_ema, load_path,
                                  self.opt['path'].get('strict_load_g',
                                                       True), 'params_ema')
            else:
                self.model_ema(0)  # copy net_g weight
            self.net_g_ema.eval()

        # define losses
        if train_opt.get('pixel_opt'):
            pixel_type = train_opt['pixel_opt'].pop('type')
            cri_pix_cls = getattr(loss_module, pixel_type)
            self.cri_pix = cri_pix_cls(**train_opt['pixel_opt']).to(
                self.device)
            self.cri_amp = AMPLoss().to(self.device)
            self.cri_phase = PhaLoss().to(self.device)
        else:
            raise ValueError('pixel loss are None.')

        # set up optimizers and schedulers
        self.setup_optimizers()
        self.setup_schedulers()

    def setup_optimizers(self):
        train_opt = self.opt['train']
        optim_params = []

        for k, v in self.net_g.named_parameters():
            if v.requires_grad:
                optim_params.append(v)
            else:
                logger = get_root_logger()
                logger.warning(f'Params {k} will not be optimized.')

        optim_type = train_opt['optim_g'].pop('type')
        if optim_type == 'Adam':
            self.optimizer_g = torch.optim.Adam(optim_params, **train_opt['optim_g'])
        elif optim_type == 'AdamW':
            self.optimizer_g = torch.optim.AdamW(optim_params, **train_opt['optim_g'])
        else:
            raise NotImplementedError(
                f'optimizer {optim_type} is not supperted yet.')
        self.optimizers.append(self.optimizer_g)

    def feed_train_data(self, data):
        self.lq = data['lq'].to(self.device)
        if 'gt' in data:
            self.gt = data['gt'].to(self.device)

        if self.mixing_flag:
            self.gt, self.lq = self.mixing_augmentation(self.gt, self.lq)

    def feed_data(self, data):
        self.lq = data['lq'].to(self.device)
        if 'gt' in data:
            self.gt = data['gt'].to(self.device)

    def optimize_parameters(self, current_iter):
        self.optimizer_g.zero_grad()
        preds = self.net_g(self.lq)

        if not isinstance(preds, list):
            preds = [preds]

        self.output = preds[-1]

        loss_dict = OrderedDict()
        # pixel loss
        l_pix = 0.
        for pred in preds:
            # l_amp = 0.01 * self.cri_amp(pred, self.gt)
            # l_pha = 0.01 * self.cri_phase(pred, self.gt)
            l_amp = 0
            l_pha = 0
            l_p = self.cri_pix(pred, self.gt)
            l_pix += l_p + l_amp + l_pha

        loss_dict['l_p'] = l_p
        loss_dict['l_pha'] = l_pha
        loss_dict['l_amp'] = l_amp

        l_pix.backward()
        if self.opt['train']['use_grad_clip']:
            torch.nn.utils.clip_grad_norm_(self.net_g.parameters(), 0.01)
        self.optimizer_g.step()

        self.log_dict = self.reduce_loss_dict(loss_dict)

        if self.ema_decay > 0:
            self.model_ema(decay=self.ema_decay)

    def pad_test(self, window_size,path = ''):        
        scale = self.opt.get('scale', 1)
        mod_pad_h, mod_pad_w = 0, 0
        _, _, h, w = self.lq.size()
        if h % window_size != 0:
            mod_pad_h = window_size - h % window_size
        if w % window_size != 0:
            mod_pad_w = window_size - w % window_size
        img = F.pad(self.lq, (0, mod_pad_w, 0, mod_pad_h), 'reflect')
        # print(self.lq)
        self.nonpad_test(img,path=path)
        _, _, h, w = self.output.size()
        self.output = self.output[:, :, 0:h - mod_pad_h * scale, 0:w - mod_pad_w * scale]


        


    def nonpad_test(self, img=None,path = None):
        if img is None:
            img = self.lq      
        if hasattr(self, 'net_g_ema'):
            self.net_g_ema.eval()
            with torch.no_grad():
                pred = self.net_g_ema(img)
            if isinstance(pred, list):
                pred = pred[-1]
            self.output = pred
        else:
            self.net_g.eval()
            with torch.no_grad():
                pred,map1,map2,map3 = self.net_g(img)
            if isinstance(pred, list):
                pred = pred[-1]
            self.output = pred
            self.net_g.train()
        print(map1.shape)
        print(path,'22222')
        mapp1 = rearrange(map1,"b h w c-> b c h w")
        mapp2 = rearrange(map2,"b h w c-> b c h w")
        mapp3 = rearrange(map3,"b h w c-> b c h w")
        channel_mean1 = torch.mean(mapp1, dim=1)
        channel_mean2 = torch.mean(mapp2, dim=1)
        channel_mean3 = torch.mean(mapp3, dim=1)
        path000 = '/data/zouz/FRFT/SNR-Aware-Low-Light-Enhance-main/visualization_decoder1'

        save_path1 = os.path.join(path000,path.split('/')[-1].replace('.png','_111.png'))
        save_path2 = os.path.join(path000,path.split('/')[-1].replace('.png','_222.png'))
        save_path3 = os.path.join(path000,path.split('/')[-1].replace('.png','_333.png'))
        print(save_path1)
        cha1 = channel_mean1.squeeze(0).cpu().numpy()
        cha2 = channel_mean2.squeeze(0).cpu().numpy()
        cha3 = channel_mean3.squeeze(0).cpu().numpy()
        print(cha1.shape,cha2.shape,cha3.shape)
        plt.imshow(cha1, cmap='gray')
        plt.axis('off')
        plt.savefig(save_path1)
        plt.show()
        plt.imshow(cha2, cmap='gray')
        plt.axis('off')
        plt.savefig(save_path2)
        plt.show()
        plt.imshow(cha3, cmap='gray')
        plt.axis('off')
        plt.savefig(save_path3)
        plt.show()
        print('123456789')

    def dist_validation(self, dataloader, current_iter, tb_logger, save_img, rgb2bgr, use_image):
        if os.environ['LOCAL_RANK'] == '0':
            return self.nondist_validation(dataloader, current_iter, tb_logger, save_img, rgb2bgr, use_image)
        else:
            return 0.

    def nondist_validation(self, dataloader, current_iter, tb_logger,
                           save_img, rgb2bgr, use_image):
        dataset_name = dataloader.dataset.opt['name']
        with_metrics = self.opt['val'].get('metrics') is not None
        if with_metrics:
            self.metric_results = {
                metric: 0
                for metric in self.opt['val']['metrics'].keys()
            }
        # pbar = tqdm(total=len(dataloader), unit='image')

        window_size = self.opt['val'].get('window_size', 0)

        if window_size:
            test = partial(self.pad_test, window_size)
        else:
            test = self.nonpad_test

        cnt = 0
        # names = []
        for idx, val_data in enumerate(dataloader):
            img_name = osp.splitext(osp.basename(val_data['lq_path'][0]))[0]
            print(val_data['lq_path'][0])
            self.feed_data(val_data)
            test(path=val_data['lq_path'][0])

            visuals = self.get_current_visuals()
            sr_img = tensor2img([visuals['result']], rgb2bgr=rgb2bgr)
            if 'gt' in visuals:
                gt_img = tensor2img([visuals['gt']], rgb2bgr=rgb2bgr)
                del self.gt

            # tentative for out of GPU memory
            del self.lq
            del self.output
            torch.cuda.empty_cache()
            print(with_metrics,'with_metrics')
            if with_metrics:
                # calculate metrics
                opt_metric = deepcopy(self.opt['val']['metrics'])
                if use_image:
                    for name, opt_ in opt_metric.items():
                        metric_type = opt_.pop('type')
                        self.metric_results[name] += getattr(
                            metric_module, metric_type)(sr_img, gt_img, **opt_)
                    cnt += 1
                else:
                    for name, opt_ in opt_metric.items():
                        metric_type = opt_.pop('type')
                        temp = getattr(
                            metric_module, metric_type)(visuals['result'], visuals['gt'], **opt_)
                        if not math.isnan(temp):
                          self.metric_results[name] += temp
                          cnt += 1
                    # print(cnt,name,self.metric_results[name])
            if save_img:
                
                if self.opt['is_train']:
                    
                    
                    save_img_path = osp.join(self.opt['path']['visualization'],
                                             img_name,
                                             f'{img_name}_{current_iter}_{temp}.png')
                    
                    save_gt_img_path = osp.join(self.opt['path']['visualization'],
                                             img_name,
                                             f'{img_name}_{current_iter}_gt.png')
                    print(save_img_path)
                else:
                    
                    save_img_path = osp.join(
                        self.opt['path']['visualization'], dataset_name,
                        f'{img_name}.png')
                    save_gt_img_path = osp.join(
                        self.opt['path']['visualization'], dataset_name,
                        f'{img_name}_gt.png')

                    
                imwrite(sr_img, save_img_path)
                imwrite(gt_img, save_gt_img_path)

            
            
        current_metric = 0.
        if with_metrics:
            for metric in self.metric_results.keys():
                self.metric_results[metric] /= cnt
                current_metric = self.metric_results[metric]

            self._log_validation_metric_values(current_iter, dataset_name,
                                               tb_logger,cnt)
        return current_metric


    def _log_validation_metric_values(self, current_iter, dataset_name,
                                      tb_logger,cnt = 200):
        log_str = f'Validation {dataset_name},\t'
        for metric, value in self.metric_results.items():
            log_str += f'\t # {metric}: {value:.4f}'
        # if cnt != 200:
            # log_str += f'\t # cnt NAN comes'
        logger = get_root_logger()
        logger.info(log_str)
        if tb_logger:
            for metric, value in self.metric_results.items():
                tb_logger.add_scalar(f'metrics/{metric}', value, current_iter)

    def get_current_visuals(self):
        out_dict = OrderedDict()
        out_dict['lq'] = self.lq.detach().cpu()
        out_dict['result'] = self.output.detach().cpu()
        if hasattr(self, 'gt'):
            out_dict['gt'] = self.gt.detach().cpu()
        return out_dict

    def save(self, epoch, current_iter):
        if self.ema_decay > 0:
            self.save_network([self.net_g, self.net_g_ema],
                              'net_g',
                              current_iter,
                              param_key=['params', 'params_ema'])
        else:
            self.save_network(self.net_g, 'net_g', current_iter)
        self.save_training_state(epoch, current_iter)
