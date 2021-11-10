from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import time

import torch
import torch.nn as nn
import torch.nn.functional as F

from progress.bar import Bar

from lib.models.data_parallel import DataParallel
from lib.utils.utils import AverageMeter
from lib.utils.post_process import ctdet_post_process


class YOLOTrainer(object):
    """MCMOT Trainer for YOLO-X. Losses are calculated in YOLO Head."""
    
    def __init__(self, opt, model, optimizer=None):
        self.opt = opt
        self.optimizer = optimizer
        self.model = model
        self.loss_stats = ['tot_loss', 'iou_loss', 'conf_loss', 'cls_loss', 'reid_loss']


    def set_device(self, gpus, chunk_sizes, device):
        dev_ids = [i for i in range(len(gpus))]
        # dev_ids = [int(x) for x in gpus]
        if len(gpus) > 1:
            self.model = DataParallel(self.model,
                                    device_ids=dev_ids,  # device_ids=gpus,
                                    chunk_sizes=chunk_sizes).to(device)
        else:
            self.model = self.model.to(device)

        for state in self.optimizer.state.values():
            for k, v in state.items():
                if isinstance(v, torch.Tensor):
                    state[k] = v.to(device=device, non_blocking=True)

    # Train an epoch
    def run_epoch(self, phase, epoch, data_loader):
        """
        :param phase:
        :param epoch:
        :param data_loader:
        :return:
        """
        model = self.model

        if phase == 'train':
            model.train()  # train phase
        else:
            if len(self.opt.gpus) > 1:
                model = self.model.module

            model.eval()  # test phase
            torch.cuda.empty_cache()

        # ----- For Train Logging
        opt = self.opt
        results = {}
        data_time, batch_time = AverageMeter(), AverageMeter()
        avg_loss_stats = {l: AverageMeter() for l in self.loss_stats}
        num_iters = len(data_loader) if opt.num_iters < 0 else opt.num_iters
        bar = Bar('{}/{}'.format(opt.task, opt.exp_id), max=num_iters)
        end = time.time()

        # ----- Iterate Through Batches
        for batch_i, (imgs, det_labels, track_ids) in enumerate(data_loader):
            if batch_i >= num_iters:
                break

            data_time.update(time.time() - end)

            # Push Data to GPU
            imgs = imgs.float().to(device=opt.device, non_blocking=True)
            det_labels = det_labels.to(device=opt.device, non_blocking=True)
            track_ids = track_ids.to(device=opt.device, non_blocking=True)
            
            # Forward with Targets
            loss, loss_stats = model.forward(imgs, (det_labels, track_ids))

            # Backwards
            if phase == 'train':
                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()

            batch_time.update(time.time() - end)
            end = time.time()

            Bar.suffix = '{phase}: [{0}][{1}/{2}]|Tot: {total:} |ETA: {eta:} '.format(
                epoch, batch_i, num_iters, phase=phase, total=bar.elapsed_td, eta=bar.eta_td)

            for l in avg_loss_stats:
                loss_value = float(loss_stats[l])
                avg_loss_stats[l].update(loss_value, imgs.size(0))
                Bar.suffix = Bar.suffix + '|{} {:.4f} '.format(l, avg_loss_stats[l].avg)

            if not opt.hide_data_time:
                Bar.suffix = Bar.suffix + '|Data {dt.val:.3f}s({dt.avg:.3f}s) ' \
                                          '|Net {bt.avg:.3f}s'.format(dt=data_time, bt=batch_time)
            if opt.print_iter > 0:
                if batch_i % opt.print_iter == 0:
                    print('{}/{}| {}'.format(opt.task, opt.exp_id, Bar.suffix))
            else:
                bar.next()

            del imgs, det_labels, track_ids, loss, loss_stats

        # Shuffle Dataset Every Epoch
        data_loader.dataset.shuffle()  # re-assign file id for each idx

        bar.finish()
        ret = {k: v.avg for k, v in avg_loss_stats.items()}
        ret['time'] = bar.elapsed_td.total_seconds() / 60.0

        return ret, results

    def save_result(self, output, batch, results):
        reg = output['reg'] if self.opt.reg_offset else None
        dets = mot_decode(heatmap=output['hm'],
                          wh=output['wh'],
                          reg=reg,
                          cat_spec_wh=self.opt.cat_spec_wh,
                          K=self.opt.K)
        dets = dets.detach().cpu().numpy().reshape(1, -1, dets.shape[2])

        dets_out = ctdet_post_process(dets.copy(),
                                      batch['meta']['c'].cpu().numpy(),  # center
                                      batch['meta']['s'].cpu().numpy(),  # scale
                                      output['hm'].shape[2],  # height
                                      output['hm'].shape[3],  # width
                                      output['hm'].shape[1])  # num_classes

        results[batch['meta']['img_id'].cpu().numpy()[0]] = dets_out[0]
        
    def debug(self, batch, output, iter_id):
        raise NotImplementedError

    def val(self, epoch, data_loader):
        return self.run_epoch('val', epoch, data_loader)

    def train(self, epoch, data_loader):
        return self.run_epoch('train', epoch, data_loader)
