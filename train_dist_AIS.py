#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time    : 2022/5/31 下午1:37
# @Author  : caden1225
# @File    : train_single.py
# @Description : BART_分布式训练
import argparse
import logging
import random
import torch.multiprocessing as mp
import torch.distributed as dist
import torch.utils.data.distributed
from torch.backends import cudnn
from pytorchtools import EarlyStopping
import os
import torch
import time
import numpy as np
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
from torch.nn.functional import log_softmax
from transformers import (
    BartConfig,
    BartForConditionalGeneration,
    get_linear_schedule_with_warmup
)
from transformers.models.bart.modeling_bart import shift_tokens_right
from utils import load_dataset

OVERSTEP = 0

def set_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--epochs', default=15, type=int, required=False, help='训练的最大轮次')
    parser.add_argument('--batch_size', default=64, type=int, required=False, help='训练的batch size')
    parser.add_argument('--log_step', default=200, type=int, required=False, help='多少步汇报一次loss')

    parser.add_argument('--data_path', default='/zhengdong3/data/data_D_json_10files', type=str, required=False, help='训练集路径')
    parser.add_argument('--save_model_path', default='/zhengdong3/projects/BART_Distributed_multi/model_dist_multi', type=str, required=False,
                        help='模型输出路径')
    parser.add_argument('--pretrained_model', default='/zhengdong3/pretrained_model/min_ppl_model', type=str, required=False,
                        help='预训练的模型的路径')
    parser.add_argument('--model_config', default='config/raw_BART_config.json', type=str, required=False,
                        help='设置模型参数')
    parser.add_argument('--log_path', default='/zhengdong3/projects/BART_Distributed_multi/log', type=str, required=False, help='训练日志存放位置')
    parser.add_argument('--tb_log_dir', default='/zhengdong3/projects/BART_Distributed_multi/tb_log', type=str, required=False, help='tensorboard训练日志存放位置')

    parser.add_argument('--max_length', default=128, type=int)
    parser.add_argument('--lr', default=3e-5, type=float)
    parser.add_argument('--adam_eps', default=1e-8, type=float)
    parser.add_argument('--warmup_steps', default=100, type=int)
    parser.add_argument('--label_smoothing', default=0.1, type=float)
    parser.add_argument('--weight_decay', default=0.01, type=float)
    parser.add_argument('--adam_betas', default='(0.9,0.999)')
    parser.add_argument('--accumulate_grad_batches', default=4, type=int)
    parser.add_argument('--gradient_clip_val', default=0.1, type=float)
    parser.add_argument('--patience', default=0, type=int)

    parser.add_argument('--vocab_path', default='/zhengdong3/pretrained_model/HF_BART_large', type=str, required=False,
                        help='词表路径')
    parser.add_argument('--val_rate', type=float, default=0.001, help='验证集比例')
    parser.add_argument('--num_workers', type=int, default=1, required=False, help="dataloader加载数据时使用的线程数量")

    # parser.add_argument('--dist_url', default='tcp://12.234.154.232:23456', type=str, help='url to set up distributed training')
    parser.add_argument('--world_size', default=-1, type=int, help='number of nodes for distributed training')
    parser.add_argument('--rank', default=-1, type=int, help='node rank for distributed training')
    parser.add_argument('--local_rank', default=-1, type=int, required=False, help='')
    parser.add_argument('--seed', default=None, type=int, required=False)
    parser.add_argument('--multi_spawn',action='store_true')

    parsed_args = parser.parse_args()
    return parsed_args


def create_logger(args):
    """
    将日志输出到日志文件和控制台
    """
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s')

    # 创建一个handler，用于写入日志文件
    file_date = time.strftime('%Y%m%d', time.localtime(time.time()))
    file_handler = logging.FileHandler(
        filename=os.path.join(args.log_path, 'train_log_' + file_date + '.log'))
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)
    logger.addHandler(file_handler)

    # 创建一个handler，用于将日志输出到控制台
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    logger.addHandler(console)

    return logger


def scaled_all_reduce(tensors):
    gpus = dist.get_world_size()
    reductions = []
    for tensor in tensors:
        reduction = torch.distributed.all_reduce(tensor, async_op=True)
        reductions.append(reduction)
    for reduction in reductions:
        reduction.wait()
    for tensor in tensors:
        tensor.mul_(1.0 / gpus)
    return tensors


def label_smoothed_nll_loss(lprobs, target, epsilon, ignore_index=0):
    '''From fairseq'''
    if target.dim() == lprobs.dim() - 1:
        target = target.unsqueeze(-1)
    nll_loss = -lprobs.gather(dim=-1, index=target)
    smooth_loss = -lprobs.sum(dim=-1, keepdim=True)
    if ignore_index is not None:
        pad_mask = target.eq(ignore_index)
        nll_loss.masked_fill_(pad_mask, 0.0)
        smooth_loss.masked_fill_(pad_mask, 0.0)
    else:
        nll_loss = nll_loss.squeeze(-1)
        smooth_loss = smooth_loss.squeeze(-1)

    nll_loss = nll_loss.mean()  # mean()? Scared to break other math.
    smooth_loss = smooth_loss.mean()
    eps_i = epsilon / lprobs.size(-1)
    loss = (1.0 - epsilon) * nll_loss + eps_i * smooth_loss
    return loss, nll_loss


def train_epoch(model, train_loader, optimizer, criterion, scheduler, logger, epoch, args, local_rank):
    pad_token_id = args.pad_token_id
    tb_writer = SummaryWriter(log_dir=args.tb_log_dir)

    model.train()
    epoch_start = time.time()
    total_loss = 0.0
    for batch_idx, batch in enumerate(train_loader):
        input_ids = batch['input_ids'].cuda(local_rank, non_blocking=True)
        attention_mask = batch['attention_mask'].cuda(local_rank, non_blocking=True)
        labels = batch['labels'].cuda(local_rank, non_blocking=True)
        decoder_input_ids = shift_tokens_right(labels, pad_token_id, decoder_start_token_id=args.sep_token_id)

        outputs = model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            decoder_input_ids=decoder_input_ids
        )
        logits = outputs.logits
        lprobs = log_softmax(logits, dim=-1)
        train_loss, nll_loss = criterion(
            lprobs=lprobs,
            target=labels,
            epsilon=args.label_smoothing,
            ignore_index=args.pad_token_id
        )
        torch.distributed.barrier()
        # if args.gradient_accumulation_steps > 1:
        #     loss = loss / args.
        reduced_loss, _ = scaled_all_reduce([train_loss, nll_loss])

        optimizer.zero_grad()
        train_loss.backward()
        optimizer.step()
        scheduler.step()

        # print(train_loss)
        total_loss += reduced_loss.item()
        del input_ids, attention_mask, labels, outputs
        global OVERSTEP
        if local_rank == 0:
            OVERSTEP += 1
        if ((batch_idx + 1) % args.log_step == 0) & (local_rank == 0):
            tb_writer.add_scalar('train_loss', train_loss.item(), global_step=OVERSTEP)
            logger.info(f" step {OVERSTEP} in Epoch {epoch} the "
                        f"current train_loss is {train_loss.item():.6f}")
    epoch_loss = total_loss / len(train_loader)

    if local_rank == 0:
        epoch_cost = time.time() - epoch_start
        logger.info("############# epoch {}: loss {} #############".format(epoch, epoch_loss))
        model_path = os.path.join(args.save_model_path, 'epoch{}'.format(epoch))
        if not os.path.exists(model_path):
            os.mkdir(model_path)
        model_to_save = model.module if hasattr(model, 'module') else model
        model_to_save.save_pretrained(model_path)
        logger.info('train epoch {} finished'.format(epoch))
        logger.info('time for one epoch: {}'.format(epoch_cost))

    return epoch_loss


def valid_epoch(model, validate_loader, criterion, logger, epoch, args, local_rank):
    if local_rank == 0:
        logger.info("validating stage")
    pad_token_id = args.pad_token_id

    model.eval()
    valid_start = time.time()
    total_loss = 0.0

    with torch.no_grad():
        for batch_idx, batch in enumerate(validate_loader):
            input_ids = batch['input_ids'].cuda(local_rank, non_blocking=True)
            attention_mask = batch['attention_mask'].cuda(local_rank, non_blocking=True)
            labels = batch['labels'].cuda(local_rank, non_blocking=True)

            decoder_input_ids = shift_tokens_right(labels, pad_token_id, decoder_start_token_id=args.sep_token_id)

            outputs = model(
                input_ids=input_ids,
                attention_mask=attention_mask,
                decoder_input_ids=decoder_input_ids
            )
            logits = outputs.logits
            lprobs = log_softmax(logits, dim=-1)

            val_loss, nll_loss = criterion(
                lprobs=lprobs,
                target=labels,
                epsilon=args.label_smoothing,
                ignore_index=args.pad_token_id
            )

            reduced_loss, _ = scaled_all_reduce([val_loss, nll_loss])

            total_loss += reduced_loss.item()
            del input_ids, attention_mask, labels, outputs
            valid_loss = total_loss / len(validate_loader)

        if args.local_rank == 0:
            valid_cost = time.time() - valid_start
            logger.info(
                "validate epoch {}: loss {}".format(epoch + 1, valid_loss))
            logger.info('time for validating one epoch: {}'.format(valid_cost))

        return valid_loss


def main():
    args = set_args()
    args.nprocs = torch.cuda.device_count()

    if args.seed is not None:
        random.seed(args.seed)
        torch.manual_seed(args.seed)
        cudnn.deterministic = True

        logging.warning('You have chosen to seed training. '
                        'This will turn on the CUDNN deterministic setting, '
                        'which can slow down your training considerably! '
                        'You may see unexpected behavior when restarting '
                        'from checkpoints.')
    if args.multi_spawn:
        args.world_size = args.nprocs * args.world_size
        mp.spawn(main_worker, nprocs=args.nprocs, args=(args.nprocs, args))
    else:
        args.local_rank = int(os.environ.get('LOCAL_RANK', args.local_rank))
        args.rank = int(os.environ.get('RANK'))
        args.world_size = int(os.environ.get('WORLD_SIZE'))
        main_worker(args.local_rank, args.nprocs, args)


def main_worker(local_rank, nprocs, args):
    logger = create_logger(args)

    if args.multi_spawn:
        args.rank = args.rank * args.nprocs + local_rank
    else:
        print(f"rank {args.rank} means the gpu num in original distributed totally")

    print("world_size---- '{}'".format(args.world_size))
    print("LOCAL_RANK---- '{}'".format(args.local_rank))
    print("RANK num---- '{}'".format(args.rank))
    print("###"*30)
    dist.init_process_group(backend='nccl',
                            world_size=args.world_size, rank=args.rank)
    print('pass the init_process_group')

    if args.pretrained_model:  # 加载预训练模型
        model = BartForConditionalGeneration.from_pretrained(args.pretrained_model)
        print("using a epoch model")
    else:  # 初始化模型
        model_config = BartConfig.from_json_file(args.model_config)
        model = BartForConditionalGeneration.from_pretrained(config=model_config)
        print(model_config)
    torch.cuda.set_device(local_rank)
    model.cuda(local_rank)
    args.pad_token_id = 0
    args.sep_token_id = 102

    args.batch_size = int(args.batch_size / args.nprocs)
    model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[local_rank])
    logger.info("use GPU {} training".format(local_rank))

    cudnn.benchmark = True

    if local_rank == 0:
        # 计算模型参数数量
        num_parameters = 0
        parameters = model.parameters()
        for parameter in parameters:
            num_parameters += parameter.numel()
        logger.info('number of model parameters: {}'.format(num_parameters))
        # 记录参数设置
        logger.info("args:{}".format(args))

    # ========= Loading Dataset ========= #
    validate_dataset, train_dataset = load_dataset(logger,args)
    val_sampler = torch.utils.data.distributed.DistributedSampler(validate_dataset)
    train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)

    train_loader = DataLoader(
        dataset=train_dataset,
        batch_size=args.batch_size,
        sampler=train_sampler,
        shuffle=False
    )
    validate_loader = DataLoader(
        dataset=validate_dataset,
        batch_size=args.batch_size,
        sampler=val_sampler,
        shuffle=False
    )

    criterion = label_smoothed_nll_loss
    early_stopping = EarlyStopping(args.patience, verbose=True, save_path=args.save_model_path)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, eps=args.adam_eps)
    t_total = len(train_loader) // args.accumulate_grad_batches * args.epochs
    scheduler = get_linear_schedule_with_warmup(
        optimizer, num_warmup_steps=args.warmup_steps, num_training_steps=t_total
    )
    logger.info(f"start training "
                f"with total step is {t_total*args.accumulate_grad_batches} with {args.epochs} epochs")

    train_losses, validate_losses = [], []
    best_val_loss = 1000
    for epoch in range(args.epochs):
        train_sampler.set_epoch(epoch)
        val_sampler.set_epoch(epoch)
        train_loss = train_epoch(
            model=model, train_loader=train_loader,
            optimizer=optimizer, criterion=criterion, scheduler=scheduler,
            logger=logger, epoch=epoch, args=args, local_rank=local_rank
        )
        train_losses.append(train_loss)

        validate_loss = valid_epoch(
            model=model, validate_loader=validate_loader, criterion=criterion,
            logger=logger, epoch=epoch, args=args, local_rank=local_rank
        )
        validate_losses.append(validate_loss)

        if (validate_loss < best_val_loss) & (args.local_rank == 0):
            best_val_loss = validate_loss
            logger.info('saving current best model for epoch {}'.format(epoch))
            ppl_model_path = os.path.join(args.save_model_path, 'min_ppl_model'.format(epoch))
            if not os.path.exists(ppl_model_path):
                os.mkdir(ppl_model_path)
            model_to_save = model.module if hasattr(model, 'module') else model
            model_to_save.save_pretrained(ppl_model_path)

            if args.patience == 0:
                continue

        early_stopping(validate_loss, model)
        if early_stopping.early_stop:
            logger.info("Early stopping")
            break
        if args.local_rank == 0:
            logger.info(f"Epoch {epoch} finished with")
            logger.info("train_loss:{}".format(np.mean(train_losses)))
            logger.info("validate_loss:{}".format(np.mean(validate_loss)))


if __name__ == '__main__':
    # os.environ["NCCL_NET_GDR_LEVEL"] = "2"
    # os.environ["NCCL_DEBUG"] = "INFO"

    main()
    # mp.spawn(main_worker, nprocs=args.nprocs, args=(args.nprocs, args))
# run with """python -m torch.distributed.launch --nnodes=2 --nproc_per_node 4 --node_rank=0 --master_addr=12.234.154.239 --master_port=33331 train_dist_AIS.py"""